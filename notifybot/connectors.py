"""Connectors layer (v1): the server-side executor of the agent's fifth tool,
``native.call`` (native macOS apps via AppleScript).

Injection-safety decision (MANDATORY, approved): every model-supplied string
travels ONLY as a positional argv item to a CONSTANT AppleScript source::

    subprocess.run(["osascript", "-e", SCRIPT_CONST, *args, ...])

The script source for each (domain, action) is a frozen constant below whose
single input channel is ``on run argv`` — user bytes NEVER appear in script
source (no f-strings, no shell, no ``run script`` of dynamic text). List
outputs are joined in-script with ``ASCII character 31`` and split here —
never comma-split.

Consent model (TCC): reminders/notes actions require ``granted`` state, which
is produced by ``probe()`` (fired from Settings › Connectors) or self-healed
by a successful action. Actions that come back denied persist ``denied`` and
raise the PT-BR consent error. ``system`` is ungated (no consent). Probes are
single-flight per domain; concurrent probes raise ``ProbeBusy`` (API maps to
409).

Deferred domains (v1 documentation table — NOT rows of GET /api/connectors;
zero secrets involved; each is read-sensitive and needs its own design pass):

==============  =========================  ==================================
Domain          Example actions            Why deferred to v2
==============  =========================  ==================================
mail            read unread count          read-sensitive mailbox access
contacts        list/search people         PII; heavier consent story
messages        list recent threads        most PII-rich surface on macOS
findmy          locate devices             location + privacy escalation
==============  =========================  ==================================

Outcome classification (actions and probes share it):
- exit 0                                              -> ok (probe: granted)
- subprocess.TimeoutExpired                           -> probe: needs_consent
                                                        action: PT-BR timeout error
   (a hanging TCC consent prompt is the common cause)
- stderr contains -1743 / "Not authorized"            -> denied (persisted;
                                                        action: PT-BR error)
- any other non-zero exit                             -> probe: denied
                                                        action: error w/ trimmed
                                                        stderr (English)

Mock hook: with env ``NOTIFYBOT_CONNECTORS_MOCK=1`` the runner seam executes a
per-process in-memory world (reminders lists, notes, volume, front app, toasts)
so smokes exercise add->list observability and the endpoints WITHOUT osascript.
"""

import json
import os
import re
import subprocess
import threading
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, NamedTuple, Optional, Tuple

from notifybot.agent_tools import _write_atomic

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Domains exposed by native.call in v1 (order == GET /api/connectors row order).
CONNECTOR_DOMAINS: Tuple[str, ...] = ("reminders", "notes", "system")

#: Whitelist: the only (domain, action) pairs dispatch accepts.
CONNECTOR_ACTIONS: Dict[str, Tuple[str, ...]] = {
    "reminders": ("list_lists", "list_reminders", "add", "complete"),
    "notes": ("search", "read", "create"),
    "system": ("volume", "front_app", "open", "toast"),
}

#: Domains that require granted consent state before ANY action runs
#: (AppleScript automation of these apps triggers macOS TCC consent).
GATED_DOMAINS: Tuple[str, ...] = ("reminders", "notes")

#: Hard per-call timeout (also enforced by subprocess.run's timeout kwarg).
CONNECTORS_TIMEOUT_S = 8.0

#: Env var that swaps the osascript runner for the in-process mock world.
CONNECTORS_MOCK_ENV = "NOTIFYBOT_CONNECTORS_MOCK"

# Server-side validation caps.
TITLE_MAX_CHARS = 512
BODY_MAX_BYTES = 16384
READ_MAX_BYTES = 16384
LIST_LIMIT_MAX = 50
LIST_LIMIT_DEFAULT = 20
LIST_NAME_MAX_CHARS = 256
QUERY_MAX_CHARS = 256

#: In-script list delimiter: ASCII character 31 (unit separator). AppleScript
#: joins list results with it; Python splits on the same byte below.
_DELIM = "\x1f"

STATUS_UNKNOWN = "unknown"
STATUS_GRANTED = "granted"
STATUS_NEEDS_CONSENT = "needs_consent"
STATUS_DENIED = "denied"
VALID_STATUSES = frozenset({STATUS_UNKNOWN, STATUS_GRANTED, STATUS_NEEDS_CONSENT, STATUS_DENIED})

# English hint strings surfaced by GET /api/connectors (Settings copy is
# English per owner rule; only chat-relayed tool errors are PT-BR).
HINT_SYSTEM = "no consent required"
HINT_GRANTED = "connected — the in-app agent may use this domain."
HINT_NEEDS_CONSENT = "macOS may show a system consent prompt — approve it while this app is focused."
HINT_DENIED = (
    "macOS previously denied automation for this domain — press Connect and "
    "approve the prompt while this app is focused."
)
HINT_UNKNOWN = HINT_NEEDS_CONSENT

# `open` target validation (system.open runs ["open", ...], never osascript).
_URL_TARGET_RE = re.compile(r"^https?://")
_APP_TARGET_RE = re.compile(r"^[A-Za-z0-9 ._-]{1,64}$")

# English (ToolFailure-style) whitelist/validation error strings.
ERR_UNKNOWN_DOMAIN = "unknown domain: {domain!r} (valid: reminders, notes, system)"
ERR_UNKNOWN_ACTION = "unknown action {action!r} for domain {domain!r} (valid: {actions})"
ERR_PARAMS_DICT = "params must be a JSON object"

# Display names used inside PT-BR user-facing error strings.
_DISPLAY_NAMES: Dict[str, str] = {"reminders": "Reminders", "notes": "Notes", "system": "System"}

# ---------------------------------------------------------------------------
# Static AppleScript sources — FROZEN constants; the ONLY input channel is
# `on run argv` (argv items are plain strings bound at run time).
# ---------------------------------------------------------------------------

REMINDERS_LIST_LISTS = """
on run argv
	set _names to {}
	tell application id "com.apple.reminders"
		set _names to name of every list
	end tell
	set AppleScript's text item delimiters to ASCII character 31
	return (_names as text)
end run
"""

REMINDERS_LIST_REMINDERS = """
on run argv
	set _listName to (item 1 of argv) as text
	set _maxCount to (item 2 of argv) as integer
	set _names to {}
	tell application id "com.apple.reminders"
		set _names to (name of reminders of list _listName whose completed is false)
	end tell
	if _maxCount is less than (count of _names) then
		set _names to items 1 through _maxCount of _names
	end if
	set AppleScript's text item delimiters to ASCII character 31
	return (_names as text)
end run
"""

REMINDERS_ADD = """
on run argv
	set _listName to (item 1 of argv) as text
	set _title to (item 2 of argv) as text
	set _yearText to (item 3 of argv) as text
	set _monthText to (item 4 of argv) as text
	set _dayText to (item 5 of argv) as text
	tell application id "com.apple.reminders"
		if _yearText is "" then
			tell list _listName
				make new reminder with properties {name:_title}
			end tell
		else
			-- Date is built component-wise from integer argv parts: parsing a
			-- date STRING ("2026-10-28") is locale-dependent; integers are not.
			set _due to current date
			set day of _due to 1
			set year of _due to (_yearText as integer)
			set month of _due to (_monthText as integer)
			set day of _due to (_dayText as integer)
			set hours of _due to 12
			set minutes of _due to 0
			set seconds of _due to 0
			tell list _listName
				make new reminder with properties {name:_title, due date:_due}
			end tell
		end if
	end tell
	return "ok"
end run
"""

REMINDERS_COMPLETE = """
on run argv
	set _listName to (item 1 of argv) as text
	set _name to (item 2 of argv) as text
	tell application id "com.apple.reminders"
		set _matches to (reminders of list _listName whose completed is false and name is _name)
		if (count of _matches) is 0 then
			return "not_found"
		end if
		set completed of item 1 of _matches to true
		return "ok" & (ASCII character 31) & (name of item 1 of _matches)
	end tell
end run
"""

NOTES_SEARCH = """
on run argv
	set _query to (item 1 of argv) as text
	set _maxCount to (item 2 of argv) as integer
	set _names to {}
	tell application id "com.apple.Notes"
		set _names to (name of notes whose name contains _query)
	end tell
	if _maxCount is less than (count of _names) then
		set _names to items 1 through _maxCount of _names
	end if
	set AppleScript's text item delimiters to ASCII character 31
	return (_names as text)
end run
"""

NOTES_READ = """
on run argv
	set _name to (item 1 of argv) as text
	tell application id "com.apple.Notes"
		set _matches to (notes whose name is _name)
		if (count of _matches) is 0 then
			return "not_found"
		end if
		return (body of item 1 of _matches)
	end tell
end run
"""

NOTES_CREATE = """
on run argv
	set _title to (item 1 of argv) as text
	set _body to (item 2 of argv) as text
	tell application id "com.apple.Notes"
		if _body is "" then
			make new note with properties {name:_title, body:_title}
		else
			make new note with properties {name:_title, body:_body}
		end if
	end tell
	return "ok"
end run
"""

SYSTEM_VOLUME_GET = """
on run argv
	return ((output volume of (get volume settings)) as text)
end run
"""

SYSTEM_VOLUME_SET = """
on run argv
	set _level to (item 1 of argv) as integer
	set volume output volume _level
	return ((output volume of (get volume settings)) as text)
end run
"""

SYSTEM_FRONT_APP = """
on run argv
	tell application id "com.apple.systemevents"
		return (name of first process whose frontmost is true)
	end tell
end run
"""

SYSTEM_TOAST = """
on run argv
	set _title to (item 1 of argv) as text
	set _body to (item 2 of argv) as text
	display notification _body with title _title
	return "ok"
end run
"""

#: Static script source per (domain, action) — the model can never influence
#: these bytes; its strings ride strictly in argv. (system.volume splits into
#: get/set script variants at argv-build time.)
SCRIPTS: Dict[Tuple[str, str], str] = {
    ("reminders", "list_lists"): REMINDERS_LIST_LISTS,
    ("reminders", "list_reminders"): REMINDERS_LIST_REMINDERS,
    ("reminders", "add"): REMINDERS_ADD,
    ("reminders", "complete"): REMINDERS_COMPLETE,
    ("notes", "search"): NOTES_SEARCH,
    ("notes", "read"): NOTES_READ,
    ("notes", "create"): NOTES_CREATE,
    ("system", "volume"): SYSTEM_VOLUME_GET,
    ("system", "volume_set"): SYSTEM_VOLUME_SET,
    ("system", "front_app"): SYSTEM_FRONT_APP,
    ("system", "toast"): SYSTEM_TOAST,
}

# Minimal probe scripts: one trivial consented touch per domain (<=8 s timeout).
PROBE_SCRIPTS: Dict[str, str] = {
    "reminders": """
on run argv
	tell application id "com.apple.reminders"
		return ((count lists) as text)
	end tell
end run
""",
    "notes": """
on run argv
	tell application id "com.apple.Notes"
		return ((count accounts) as text)
	end tell
end run
""",
    # system needs no consent; the volume read is a harmless osascript sanity probe.
    "system": SYSTEM_VOLUME_GET,
}

# Bundle ids keep the scripts immune to pt-BR app-name localization. (system's
# front_app touches System Events; volume uses scripting additions only.)
BUNDLE_IDS: Dict[str, str] = {
    "reminders": "com.apple.reminders",
    "notes": "com.apple.Notes",
    "system": "com.apple.systemevents",
}


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _hint_for(status: str) -> str:
    if status == STATUS_GRANTED:
        return HINT_GRANTED
    if status == STATUS_NEEDS_CONSENT:
        return HINT_NEEDS_CONSENT
    if status == STATUS_DENIED:
        return HINT_DENIED
    return HINT_UNKNOWN


class _Outcome(NamedTuple):
    """Runner seam result shape (CompletedProcess-compatible subset)."""

    returncode: int
    stdout: str
    stderr: str


def _default_runner(cmd: List[str], timeout: float) -> Any:
    """Production osascript bridge: list-form argv (no shell, ever)."""
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def _is_denial_stderr(stderr: str) -> bool:
    """osascript TCC-denial markers (exit -1743 family)."""
    lowered = (stderr or "").lower()
    return "-1743" in lowered or "not authorized" in lowered


def _split_delimited(stdout: str) -> List[str]:
    """Split a delimiter-joined script result; empty strings dropped."""
    return [item for item in (stdout or "").split(_DELIM) if item != ""]


def _truncate_bytes(text: str, cap: int) -> str:
    """Byte-cap a string on a UTF-8 boundary (never splits a code point)."""
    encoded = text.encode("utf-8")
    if len(encoded) <= cap:
        return text
    return encoded[:cap].decode("utf-8", errors="ignore")


def _parse_level_stdout(stdout: str) -> int:
    try:
        return int((stdout or "").strip())
    except ValueError:
        raise ConnectorError("volume result was not an integer (osascript output changed)") from None


class ConnectorError(Exception):
    """User-visible connector failure. Message is PT-BR where user-facing
    (consent/denial/timeout/not-found); English for validation/infrastructure."""


class _ScriptFailure(Exception):
    """Internal (pre-message) failure classification from the runner seam."""

    def __init__(self, kind: str, returncode: int, stderr: str):
        super().__init__(kind)
        self.kind = kind  # "timeout" | "denied" | "error"
        self.returncode = int(returncode)
        self.stderr = stderr


class ProbeBusy(Exception):
    """Raised when a probe is already running for the same domain (API: 409)."""


class ConnectorsStateStore:
    """Per-domain consent state under ``<config_dir>/data/connectors_state.json``
    (sibling of ``data/agent_sessions/``). Runtime-only file — never stores
    secrets (v1 domains hold none), never enters git (gitignored)."""

    def __init__(self, config_dir: str):
        self.config_dir = config_dir
        self.path = os.path.join(config_dir, "data", "connectors_state.json")
        self._lock = threading.Lock()

    def read(self) -> Dict[str, Dict[str, Optional[str]]]:
        """Fresh read: per-domain ``{status, last_probe}``; absent/bad values
        degrade to ``unknown``/None. Atomic writes keep readers torn-file-free."""
        with self._lock:
            raw = self._read_unlocked()
        rows: Dict[str, Dict[str, Optional[str]]] = {}
        for domain in CONNECTOR_DOMAINS:
            entry = raw.get(domain) if isinstance(raw.get(domain), dict) else {}
            status = entry.get("status")
            last_probe = entry.get("last_probe")
            rows[domain] = {
                "status": status if status in VALID_STATUSES else STATUS_UNKNOWN,
                "last_probe": last_probe if isinstance(last_probe, str) else None,
            }
        return rows

    def record(self, domain: str, status: str) -> Dict[str, Any]:
        """Persist one domain under the write lock (read-modify-write +
        ``_write_atomic``); returns the stored ``{status, last_probe}`` row."""
        if domain not in CONNECTOR_DOMAINS:
            raise ValueError(f"unknown connector domain: {domain!r}")
        if status not in VALID_STATUSES:
            raise ValueError(f"invalid connector status: {status!r}")
        stamp = _utcnow_iso()
        with self._lock:
            data = self._read_unlocked()
            data[domain] = {"status": status, "last_probe": stamp}
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            _write_atomic(self.path, json.dumps(data, indent=2, ensure_ascii=False))
        return {"status": status, "last_probe": stamp}

    def _read_unlocked(self) -> Dict[str, Any]:
        if not os.path.exists(self.path):
            return {}
        try:
            with open(self.path, "r", encoding="utf-8") as read_file:
                data = json.load(read_file)
        except (OSError, ValueError):
            return {}  # a corrupted store rebuilds on the next record()
        return data if isinstance(data, dict) else {}


# ---------------------------------------------------------------------------
# Argument validation helpers (all errors English, ToolFailure-style)
# ---------------------------------------------------------------------------


def _as_string(value: Any, field: str, field_max: Optional[int] = None) -> str:
    """Coerce a model-supplied JSON value to a validated, non-empty string."""
    if value is None or isinstance(value, (bool, dict, list)):
        raise ConnectorError(f"'{field}' must be a non-empty string")
    if isinstance(value, (int, float)):
        value = str(value)
    if not isinstance(value, str):
        raise ConnectorError(f"'{field}' must be a non-empty string")
    text = value.strip()
    if text == "":
        raise ConnectorError(f"'{field}' must be a non-empty string")
    if field_max is not None and len(text) > field_max:
        raise ConnectorError(f"'{field}' is limited to {field_max} characters")
    return text


def _as_limit(value: Any) -> int:
    if value is None:
        return LIST_LIMIT_DEFAULT
    if isinstance(value, bool):
        raise ConnectorError(f"'limit' must be an integer between 1 and {LIST_LIMIT_MAX}")
    if isinstance(value, float):
        if not value.is_integer():
            raise ConnectorError(f"'limit' must be an integer between 1 and {LIST_LIMIT_MAX}")
        value = int(value)
    if isinstance(value, int):
        limit = value
    elif isinstance(value, str) and value.strip().isdigit():
        limit = int(value.strip())
    else:
        raise ConnectorError(f"'limit' must be an integer between 1 and {LIST_LIMIT_MAX}")
    if not (1 <= limit <= LIST_LIMIT_MAX):
        raise ConnectorError(f"'limit' must be an integer between 1 and {LIST_LIMIT_MAX}")
    return limit


def _parse_due(value: Any) -> Optional[Tuple[int, int, int]]:
    """``YYYY-MM-DD`` -> (year, month, day); None/'' -> None (no due date)."""
    if value is None:
        return None
    if isinstance(value, (bool, dict, list)):
        raise ConnectorError("'due' must be a string formatted YYYY-MM-DD")
    text = str(value).strip()
    if text == "":
        return None
    match = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", text)
    if not match:
        raise ConnectorError("'due' must be a string formatted YYYY-MM-DD")
    year, month, day = int(match.group(1)), int(match.group(2)), int(match.group(3))
    if not (1 <= year <= 9999 and 1 <= month <= 12 and 1 <= day <= 31):
        raise ConnectorError("'due' month must be 01-12 and day 01-31")
    return year, month, day


def _as_level(value: Any) -> int:
    if isinstance(value, bool) or value is None:
        raise ConnectorError("'level' must be an integer between 0 and 100")
    if isinstance(value, float):
        if not value.is_integer():
            raise ConnectorError("'level' must be an integer between 0 and 100")
        value = int(value)
    if isinstance(value, int):
        level = value
    elif isinstance(value, str) and value.strip().isdigit():
        level = int(value.strip())
    else:
        raise ConnectorError("'level' must be an integer between 0 and 100")
    if not (0 <= level <= 100):
        raise ConnectorError("'level' must be an integer between 0 and 100")
    return level


def _required_params(params: Dict[str, Any], fields: Tuple[str, ...]) -> None:
    for name in fields:
        if name not in params or params.get(name) is None:
            raise ConnectorError(f"'{name}' is required")


def _at_most_one(params: Dict[str, Any], fields: Tuple[str, ...], error: str) -> str:
    present = [name for name in fields if params.get(name) not in (None, "")]
    if len(present) != 1:
        raise ConnectorError(error)
    return present[0]


def _body_bytes_cap(value: Any, field: str) -> str:
    """Cap a body at BODY_MAX_BYTES (server-side; context cap applies on top)."""
    text = _as_string(value, field)
    return _truncate_bytes(text, BODY_MAX_BYTES)


class _ArgvPlan(NamedTuple):
    """A fully validated native.call invocation: the exact command list plus
    how to normalize the runner's stdout into the tool result."""

    cmd: List[str]
    kind: str = "ok"  # ok | split | complete | read | raw | level_get | level_set
    post: Optional[Callable[[dict], dict]] = None
    complete_list: str = ""
    complete_query: str = ""
    read_name: str = ""


# ---------------------------------------------------------------------------
# ConnectorManager
# ---------------------------------------------------------------------------


class ConnectorManager:
    """Whitelist + consent gate + argv-bound osascript executor for native.call.

    One per server process (BotApiRuntime lazy singleton; ToolRegistry owns a
    reference so ``native.call`` never rebuilds it).
    """

    _Runner = Callable[[List[str], float], Any]

    def __init__(self, config_dir: str, runner: Optional[_Runner] = None):
        self.config_dir = config_dir
        self.state = ConnectorsStateStore(config_dir)
        # Runner seam: explicit injection wins (tests); else the in-process
        # mock world when NOTIFYBOT_CONNECTORS_MOCK=1; else osascript.
        self._mock_mode = runner is None and os.environ.get(CONNECTORS_MOCK_ENV, "") == "1"
        if runner is not None:
            self._runner: Callable[[List[str], float], Any] = runner
        elif self._mock_mode:
            self._runner = self._mock_runner
        else:
            self._runner = _default_runner
        self._probe_locks: Dict[str, threading.Lock] = {d: threading.Lock() for d in CONNECTOR_DOMAINS}
        # In-memory mock world (per manager == per server process; tests get a
        # fresh world per manager instance).
        self._mock_lock = threading.Lock()
        self._mock_lists: Dict[str, List[Dict[str, Any]]] = {}
        self._mock_notes: List[Dict[str, str]] = []
        self._mock_volume = 50
        self._mock_front_app = "Finder"
        self._mock_last_toast: Optional[Tuple[str, str]] = None

    # ---- public API -------------------------------------------------------

    def rows(self) -> dict:
        """GET /api/connectors payload: per-domain status/granted/last_probe/hint.
        ``system`` is always granted (no consent required)."""
        store_rows = self.state.read()
        connectors: Dict[str, Any] = {}
        for domain in CONNECTOR_DOMAINS:
            if domain == "system":
                status = STATUS_GRANTED
            else:
                status = store_rows[domain]["status"]
            connectors[domain] = {
                "status": status,
                "granted": status == STATUS_GRANTED,
                "last_probe": store_rows[domain]["last_probe"],
                "hint": HINT_SYSTEM if domain == "system" else _hint_for(status),
            }
        return {"connectors": connectors}

    def run_action(self, domain: str, action: str, params: Any) -> dict:
        """Validate -> consent-gate -> argv-build -> run -> classify. Raises
        ConnectorError carrying the final user-visible message on failure."""
        if domain not in CONNECTOR_ACTIONS:
            raise ConnectorError(ERR_UNKNOWN_DOMAIN.format(domain=domain))
        if action not in CONNECTOR_ACTIONS[domain]:
            raise ConnectorError(
                ERR_UNKNOWN_ACTION.format(
                    domain=domain, action=action, actions=", ".join(CONNECTOR_ACTIONS[domain])
                )
            )
        if not isinstance(params, dict):
            raise ConnectorError(ERR_PARAMS_DICT)

        plan = self._build_argv(domain, action, params)

        if domain in GATED_DOMAINS and self.state.read()[domain]["status"] != STATUS_GRANTED:
            raise ConnectorError(
                f"{_DISPLAY_NAMES[domain]} ainda não está conectado — "
                f"conecte {domain} em Settings › Connectors."
            )

        try:
            outcome = self._execute(plan.cmd)
        except _ScriptFailure as failure:
            if failure.kind == "denied" and domain in GATED_DOMAINS:
                self.state.record(domain, STATUS_DENIED)
            raise self._action_failure_error(domain, failure) from None

        result = self._normalize(domain, plan, outcome)

        # Success self-heal: a gated domain that just worked is granted.
        if domain in GATED_DOMAINS:
            current = self.state.read()[domain]["status"]
            if current != STATUS_GRANTED:
                self.state.record(domain, STATUS_GRANTED)
        return result

    def probe(self, domain: str) -> dict:
        """Single-flight per-domain probe; returns ``{ok, domain, status,
        last_probe}``. Never raises for app-level outcomes (a hang classifies
        needs_consent; a -1743/error classifies denied)."""
        if domain not in CONNECTOR_DOMAINS:
            raise ConnectorError(ERR_UNKNOWN_DOMAIN.format(domain=domain))
        lock = self._probe_locks[domain]
        if not lock.acquire(blocking=False):
            raise ProbeBusy(f"a probe for {domain!r} is already running")
        try:
            try:
                self._execute(["osascript", "-e", PROBE_SCRIPTS[domain]])
                status = STATUS_GRANTED
            except _ScriptFailure as failure:
                status = STATUS_NEEDS_CONSENT if failure.kind == "timeout" else STATUS_DENIED
            stored = self.state.record(domain, status)
            return {"ok": True, "domain": domain, "status": status, "last_probe": stored["last_probe"]}
        finally:
            lock.release()

    # ---- internals -----------------------------------------------------------

    def _execute(self, cmd: List[str]) -> _Outcome:
        """One dispatch of the runner seam (mock-aware), normalized to _Outcome
        or classified as _ScriptFailure. Never returns a failing outcome."""
        if self._mock_mode:
            return self._mock_runner(cmd, CONNECTORS_TIMEOUT_S)
        try:
            outcome = self._runner(cmd, CONNECTORS_TIMEOUT_S)
        except subprocess.TimeoutExpired as exc:
            raise _ScriptFailure("timeout", 0, "") from exc
        except OSError as exc:
            raise ConnectorError(f"osascript execution failed: {exc}") from exc
        stdout = outcome.stdout if isinstance(outcome.stdout, str) else ("" if outcome.stdout is None else str(outcome.stdout))
        stderr = outcome.stderr if isinstance(outcome.stderr, str) else ("" if outcome.stderr is None else str(outcome.stderr))
        returncode = int(outcome.returncode)
        if returncode == 0:
            return _Outcome(returncode=0, stdout=stdout, stderr=stderr)
        kind = "denied" if _is_denial_stderr(stderr) else "error"
        raise _ScriptFailure(kind, returncode, stderr)

    @staticmethod
    def _action_failure_error(domain: str, failure: _ScriptFailure) -> ConnectorError:
        """Map a classified script failure to the FINAL user-visible message
        (PT-BR for the consent family; English for script errors)."""
        display = _DISPLAY_NAMES[domain]
        if failure.kind == "timeout":
            return ConnectorError(
                f"{display} não respondeu em {int(CONNECTORS_TIMEOUT_S)}s — pode ter aparecido "
                "um prompt de consentimento do macOS pendente (aprove-o com o aplicativo em "
                "foco) ou o app ainda está iniciando; tente de novo em instantes ou use o botão "
                "Connect em Settings › Connectors."
            )
        if failure.kind == "denied":
            return ConnectorError(
                f"O macOS negou a automação de {display} (-1743 / não autorizado). "
                f"Conecte {domain} em Settings › Connectors e aprove o prompt de consentimento "
                "com o aplicativo em foco."
            )
        detail = _truncate_bytes(failure.stderr.strip(), 200) or "(sem stderr)"
        return ConnectorError(
            f"{display} action failed (exit {failure.returncode}): {detail}"
        )

    @staticmethod
    def _normalize(domain: str, plan: _ArgvPlan, outcome: _Outcome) -> dict:
        del domain  # reserved for per-domain normalization tweaks
        if plan.kind == "ok":
            return dict(plan.post({})) if plan.post is not None else {"ok": True}
        if plan.kind == "level_get":
            return {"level": _parse_level_stdout(outcome.stdout)}
        if plan.kind == "level_set":
            level = _parse_level_stdout(outcome.stdout)
            return {"ok": True, "level": level}
        if plan.kind == "split":
            assert plan.post is not None
            return plan.post({"items": _split_delimited(outcome.stdout)})
        if plan.kind == "complete":
            return ConnectorManager._parse_complete_stdout(outcome.stdout, plan.complete_query, plan.complete_list)
        if plan.kind == "read":
            return ConnectorManager._parse_read_stdout(outcome.stdout, plan.read_name)
        # "raw" — single trimmed text (front_app)
        text = (outcome.stdout or "").strip()
        if text == "":
            raise ConnectorError("the macOS app returned an empty result")
        return {"app": text}

    @staticmethod
    def _parse_complete_stdout(stdout: str, query: str, list_name: str) -> dict:
        if (stdout or "").strip() == "not_found":
            raise ConnectorError(
                f"Lembrete '{query}' não encontrado na lista '{list_name}' "
                "(verifique o nome exato e se ele ainda está pendente)."
            )
        items = _split_delimited(stdout)
        completed = items[1] if len(items) > 1 else query
        return {"ok": True, "completed": completed}

    @staticmethod
    def _parse_read_stdout(stdout: str, name: str) -> dict:
        if (stdout or "").strip() == "not_found":
            raise ConnectorError(
                f"Nota '{name}' não encontrada (a nota deve existir com esse nome exato)."
            )
        return {"name": name, "body": _truncate_bytes((stdout or "").strip(), READ_MAX_BYTES)}

    # ---- argv builders (validate + freeze the exact command) ------------------

    def _build_argv(self, domain: str, action: str, params: Dict[str, Any]) -> _ArgvPlan:
        if domain == "reminders":
            return self._build_reminders(action, params)
        if domain == "notes":
            return self._build_notes(action, params)
        return self._build_system(action, params)

    def _build_reminders(self, action: str, params: Dict[str, Any]) -> _ArgvPlan:
        if action == "list_lists":
            return _ArgvPlan(
                ["osascript", "-e", REMINDERS_LIST_LISTS],
                kind="split",
                post=lambda raw: {"lists": raw["items"]},
            )
        if action == "list_reminders":
            _required_params(params, ("list",))
            list_name = _as_string(params.get("list"), "list", LIST_NAME_MAX_CHARS)
            limit = _as_limit(params.get("limit"))
            return _ArgvPlan(
                ["osascript", "-e", REMINDERS_LIST_REMINDERS, list_name, str(limit)],
                kind="split",
                post=lambda raw: {"list": list_name, "reminders": raw["items"]},
            )
        if action == "add":
            _required_params(params, ("list", "title"))
            list_name = _as_string(params.get("list"), "list", LIST_NAME_MAX_CHARS)
            title = _as_string(params.get("title"), "title", TITLE_MAX_CHARS)
            due = _parse_due(params.get("due"))
            if due is None:
                argv = [list_name, title, "", "", ""]
                due_out: Optional[str] = None
            else:
                year, month, day = due
                argv = [list_name, title, str(year), f"{month:02d}", f"{day:02d}"]
                due_out = f"{year:04d}-{month:02d}-{day:02d}"

            def post(raw: dict, _list_name: str = list_name, _title: str = title, _due: Optional[str] = due_out) -> dict:
                del raw
                result = {"ok": True, "list": _list_name, "title": _title}
                if _due is not None:
                    result["due"] = _due
                return result

            return _ArgvPlan(["osascript", "-e", REMINDERS_ADD, *argv], kind="ok", post=post)
        # complete — exactly one of title/id (both matched against reminder names)
        _required_params(params, ("list",))
        list_name = _as_string(params.get("list"), "list", LIST_NAME_MAX_CHARS)
        matched_field = _at_most_one(
            params,
            ("title", "id"),
            "complete requires exactly one of 'title' or 'id' (matched against reminder names)",
        )
        target = _as_string(params.get(matched_field), matched_field, TITLE_MAX_CHARS)
        return _ArgvPlan(
            ["osascript", "-e", REMINDERS_COMPLETE, list_name, target],
            kind="complete",
            complete_list=list_name,
            complete_query=target,
        )

    def _build_notes(self, action: str, params: Dict[str, Any]) -> _ArgvPlan:
        if action == "search":
            _required_params(params, ("query",))
            query = _as_string(params.get("query"), "query", QUERY_MAX_CHARS)
            limit = _as_limit(params.get("limit"))
            return _ArgvPlan(
                ["osascript", "-e", NOTES_SEARCH, query, str(limit)],
                kind="split",
                post=lambda raw: {"notes": raw["items"]},
            )
        if action == "read":
            _required_params(params, ("name",))
            name = _as_string(params.get("name"), "name", LIST_NAME_MAX_CHARS)
            return _ArgvPlan(
                ["osascript", "-e", NOTES_READ, name],
                kind="read",
                read_name=name,
            )
        # create (default account/folder; title becomes the note name)
        _required_params(params, ("title",))
        title = _as_string(params.get("title"), "title", TITLE_MAX_CHARS)
        body = ""
        if params.get("body") not in (None, ""):
            body = _body_bytes_cap(params.get("body"), "body")

        def post(raw: dict, _title: str = title) -> dict:
            del raw
            return {"ok": True, "name": _title}

        return _ArgvPlan(["osascript", "-e", NOTES_CREATE, title, body], kind="ok", post=post)

    def _build_system(self, action: str, params: Dict[str, Any]) -> _ArgvPlan:
        if action == "volume":
            if params.get("level") in (None, ""):
                return _ArgvPlan(["osascript", "-e", SYSTEM_VOLUME_GET], kind="level_get")
            level = _as_level(params.get("level"))
            return _ArgvPlan(["osascript", "-e", SYSTEM_VOLUME_SET, str(level)], kind="level_set")
        if action == "front_app":
            return _ArgvPlan(["osascript", "-e", SYSTEM_FRONT_APP], kind="raw")
        if action == "open":
            # open runs ["open", ...] directly (NO osascript), validated hard:
            # http(s) URLs -> plain open; app names -> open -a; rest rejected.
            target = _as_string(params.get("target"), "target", 1024)
            if _URL_TARGET_RE.match(target):
                cmd = ["open", target]
            elif _APP_TARGET_RE.match(target) and not target.startswith("-"):
                cmd = ["open", "-a", target]
            else:
                raise ConnectorError(
                    "invalid open target: only http(s) URLs or app names "
                    "(letters, digits, space, '.', '_', '-', max 64 chars, "
                    "never starting with '-') are allowed"
                )

            def post(raw: dict, _target: str = target) -> dict:
                del raw
                return {"ok": True, "target": _target}

            return _ArgvPlan(cmd, kind="ok", post=post)
        # toast
        title = _as_string(params.get("title"), "title", TITLE_MAX_CHARS)
        body = _truncate_bytes(_as_string(params.get("body"), "body"), BODY_MAX_BYTES)

        def post(raw: dict, _title: str = title) -> dict:
            del raw
            return {"ok": True, "title": _title}

        return _ArgvPlan(["osascript", "-e", SYSTEM_TOAST, title, body], kind="ok", post=post)

    # ---- mock world ------------------------------------------------------------

    def reset_mock_world(self) -> None:
        """Reset the in-memory mock world (test/smoke convenience)."""
        with self._mock_lock:
            self._mock_lists = {}
            self._mock_notes = []
            self._mock_volume = 50
            self._mock_front_app = "Finder"
            self._mock_last_toast = None

    def _mock_runner(self, cmd: List[str], timeout: float) -> _Outcome:
        """In-process osascript replacement: interprets the SAME frozen scripts
        against the in-memory world (add -> list stays genuinely observable).
        Equality-compares cmd[2] against the constants."""
        del timeout
        script = cmd[2] if len(cmd) > 2 else ""
        argv = list(cmd[3:])
        with self._mock_lock:
            if script == REMINDERS_ADD:
                list_name, title, year, month, day = (argv + [""] * 5)[:5]
                bucket = self._mock_lists.setdefault(list_name, [])
                bucket.append(
                    {
                        "title": title,
                        "completed": False,
                        "due": None if year == "" else "%s-%s-%s" % (year, month, day),
                    }
                )
                return _Outcome(0, "ok", "")
            if script == REMINDERS_COMPLETE:
                list_name, target = argv[:2]
                for item in self._mock_lists.get(list_name, []):
                    if not item["completed"] and item["title"].lower() == target.lower():
                        item["completed"] = True
                        return _Outcome(0, "ok" + _DELIM + item["title"], "")
                return _Outcome(0, "not_found", "")
            if script == REMINDERS_LIST_LISTS:
                return _Outcome(0, _DELIM.join(sorted(self._mock_lists.keys())), "")
            if script == REMINDERS_LIST_REMINDERS:
                list_name, limit_text = argv[:2]
                names = [
                    item["title"]
                    for item in self._mock_lists.get(list_name, [])
                    if not item["completed"]
                ][: int(limit_text)]
                return _Outcome(0, _DELIM.join(names), "")
            if script == NOTES_SEARCH:
                query, limit_text = argv[:2]
                needle = query.lower()
                names = [
                    note["name"]
                    for note in self._mock_notes
                    if needle in note["name"].lower()
                ][: int(limit_text)]
                return _Outcome(0, _DELIM.join(names), "")
            if script == NOTES_READ:
                name = argv[0]
                for note in self._mock_notes:
                    if note["name"].lower() == name.lower():
                        return _Outcome(0, note["body"], "")
                return _Outcome(0, "not_found", "")
            if script == NOTES_CREATE:
                title, body = (argv + [""])[:2]
                self._mock_notes.append({"name": title, "body": body if body else title})
                return _Outcome(0, "ok", "")
            if script == SYSTEM_VOLUME_GET:
                return _Outcome(0, str(self._mock_volume), "")
            if script == SYSTEM_VOLUME_SET:
                self._mock_volume = int(argv[0])
                return _Outcome(0, str(self._mock_volume), "")
            if script == SYSTEM_FRONT_APP:
                return _Outcome(0, self._mock_front_app, "")
            if script == SYSTEM_TOAST:
                self._mock_last_toast = (argv[0], argv[1] if len(argv) > 1 else "")
                return _Outcome(0, "ok", "")
            # Probe scripts (and anything else): trivial success.
            return _Outcome(0, "0", "")