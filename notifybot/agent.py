"""In-process agent tool loop for the desktop Chat tab.

One turn = load/create session -> loop (<= MAX_TOOL_TURNS): global budget
check -> LLM call -> strict one-JSON-per-turn parse (reminder retry) -> tool
dispatch or final answer -> append-only JSONL persistence
under ``<config_dir>/data/agent_sessions/<session_id>.jsonl``.

Context hygiene + drift guards (post-8f0ae727 fix):

- history tool rows re-send args with LARGE values redacted to a short
  ``args_summary`` marker (the full args stay only in the session jsonl);
- when the last successful tool is ``dashboard.create``, the next prompt
  carries a targeted continuation recipe (the exact ``server.serve`` template
  with the real artifact id) instead of only the generic protocol reminder;
- every raw model output is persisted BEFORE parsing as an ``assistant_raw``
  row (``raw_model_output`` capped at ~8 KB) for drift post-mortems;
- prior assistant ANSWERS longer than ~240 chars are truncated in the model
  CONTEXT only (first ~200 chars + ``…[answer truncated N chars in context]``);
  the full answer stays in the session jsonl on disk and in the UI (round 3);
- when the request continues a session that already has prior turns, EVERY
  outgoing prompt of the turn re-states the one-JSON protocol in one standing
  system line (``STANDING_PROTOCOL_REMINDER``) before the model's first call.

Deterministic pipeline completion (post-2: the "drifted after create" class
of failure cannot lose the delivered dashboard anymore):

- the system prompt is date-grounded (``Current local date:`` line computed
  at build time) so "este mês" style requests stop hitting wrong months;
- when a model-driven ``dashboard.create`` succeeds, the LOOP itself
  deterministically executes ``server.serve {action: ensure}`` (appended as a
  ``auto_serve`` step WITHOUT a model call and WITHOUT consuming a
  MAX_TOOL_TURNS slot) and re-prompts the model for ONLY the final answer;
- when a completed create+serve pair exists and the model then drifts/fails
  to produce the answer (guard paths), the loop synthesizes the final PT-BR
  answer from the tool results (``synthesize_dashboard_answer``) and marks
  the answer record ``auto_complete`` — the user-facing pipeline completes.

LLM injection: ``run_turn(runtime, session_id, message, llm_fn=None)``.
``llm_fn(messages) -> str`` — an exception raised by ``llm_fn`` is converted
into a graceful PT-BR error answer (the caller keeps HTTP 200; the loop
itself never turns an LLM failure into a 500). The production Ollama path
(``_ollama_chat``) retries TRANSIENT transport failures (URLError, 5xx
HTTPError, socket/TimeoutError) 2 extra attempts with 2 s linear backoff
before giving up; per-attempt timeout stays ``config.ollama.timeout``.

Mock hook: when the env var ``NOTIFYBOT_SMOKE_AGENT_MOCK=1`` is set,
``default_llm_fn`` selects a deterministic scripted mock LLM that walks the
full dashboard pipeline (app.data -> dashboard.create -> the loop's
auto-executed server.serve -> answer with urls) for any "dashboard" request
and uses ``notify.test`` for "test notification" requests. This is a
TEST/SMOKE-ONLY hook: unit tests inject their own ``llm_fn`` transcript
stubs; the API smoke (``scripts/smoke_api.py``) uses the env hook to
exercise the server-side pipeline without a live Ollama model.
"""

import copy
import json
import logging
import os
import re
import socket
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from notifybot.agent_tools import (
    MAX_TOOL_TURNS,
    SESSION_CONTEXT_MESSAGES,
    TOOL_RESULT_MAX_BYTES,
    TURN_BUDGET_S,
)
from notifybot.classifier import clean_json_text

logger = logging.getLogger("notification_bot")

SESSION_ID_RE = re.compile(r"^[A-Za-z0-9-]{1,64}$")
AGENT_MESSAGE_MAX_BYTES = 8192
MOCK_ENV = "NOTIFYBOT_SMOKE_AGENT_MOCK"

# Raw model outputs are persisted per LLM call (drift post-mortems use real
# data), each row capped at ~8 KB; bigger outputs keep their head + marker.
RAW_MODEL_OUTPUT_MAX_BYTES = 8192
# Tool args larger than this are REDACTED in the model context (a size marker
# replaces the value); the full args live only in the session jsonl.
TOOL_ARG_INLINE_MAX_CHARS = 160
# Prior assistant ANSWERS longer than this are truncated in the model context
# (first PRIOR_ANSWER_KEPT_CHARS kept + explicit marker); the FULL text stays
# in the session jsonl on disk and in the UI — only the model's context
# shrinks. Evidence (session f1b57e96, round 3): the long assistant answer of
# the previous turn (~450+ chars of full dashboard prose echoed back) made the
# NEXT fresh ask drift out of protocol on its very first model turn. Applies
# to guard-error and successful answers alike.
PRIOR_ANSWER_INLINE_MAX_CHARS = 240
PRIOR_ANSWER_KEPT_CHARS = 200

# Deterministic completion phases (additive session-record fields):
# - ``auto_serve`` marks the loop-executed server.serve step (no model call,
#   no MAX_TOOL_TURNS slot) right after a model-driven dashboard.create ok;
# - ``auto_complete`` marks a turn whose final answer was SYNTHESIZED by the
#   loop (model drifted/failed while create+serve had already succeeded).
AUTO_SERVE_PHASE = "auto_serve"
AUTO_COMPLETE_PHASE = "auto_complete"

# Ollama transport resilience: per agent LLM call, transient transport
# failures (URLError / HTTPError 5xx / socket.timeout / TimeoutError) get 2
# EXTRA attempts with a 2 s linear backoff; the per-attempt timeout stays
# config.ollama.timeout. Non-transport failures (e.g. HTTPError 4xx) and
# non-transport exceptions surface immediately (hard-failure guard path).
OLLAMA_TRANSPORT_RETRIES = 2
OLLAMA_RETRY_BACKOFF_S = 2.0

# Live turn progress side channel (GET /api/agent/active-turn): the model's
# streamed thinking/raw tail is capped at this many chars while streaming
# (longer buffers keep the LAST chars, ellipsis-prefixed).
RAW_PARTIAL_MAX_CHARS = 400

PROMPT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "agent_prompt.md")

REMINDER_PREFIX = "[protocol reminder] "

PROTOCOL_REMINDER = REMINDER_PREFIX + (
    "Your previous reply was NOT one valid protocol JSON object. Reply again with "
    'EXACTLY ONE JSON object \u2014 either {"tool": "<name>", "args": {...}} or '
    '{"answer": "<pt-br text>", "urls": [...]} \u2014 with no prose and no markdown fences.'
)

# Targeted recipe re-anchoring (drift fix for the post-dashboard.create turn):
# when the last successful tool in context is dashboard.create, this template
# (with the REAL artifact id substituted) replaces the generic reminder —
# generic-only reminders let glm-5.3-flash drift into prose twice and kill the
# pipeline before server.serve/answer.
SERVE_CONTINUATION_TEMPLATE = (
    REMINDER_PREFIX
    + "The tool result shows the dashboard was created successfully. Reply NOW with "
    "EXACTLY ONE JSON object \u2014 no prose, no markdown fences. The required next step is:\n"
    '{"tool": "server.serve", "args": {"action": "ensure", "artifact_id": "{artifact_id}"}}\n'
    "After the server.serve result arrives, reply with the final answer shape:\n"
    '{"answer": "<short pt-br text>", "urls": ["<the url from the server.serve result>"]}. '
    "Never repeat dashboard.create and never call other tools before server.serve. "
    "Dashboards must be responsive (grid repeat(auto-fit, minmax(220px, 1fr)) or fluid columns; "
    "never fixed px column totals; usable at ~440px pane width)."
)

# Finalize recipe (recipe completion, post-2): after the LOOP has executed the
# server.serve step automatically, this template (with the served URL
# substituted) re-anchors the model for ONLY the final answer+urls — it is
# the last model task of the turn and must not include any more tool calls.
FINALIZE_CONTINUATION_TEMPLATE = (
    REMINDER_PREFIX
    + "The pipeline is ALREADY COMPLETE: dashboard.create succeeded and the "
    "application loop has just executed the server.serve ensure step for you "
    "(see the last tool_result above; its result carries the final URL). Do NOT call "
    "any tool again. Reply NOW with EXACTLY ONE JSON object \u2014 no prose, no markdown "
    "fences \u2014 in the final answer shape:\n"
    '{"answer": "<short pt-br text saying the dashboard is ready>", "urls": ["{serve_url}"]}'
)

# Standing protocol reminder (session-2 drift fix, round 3): when the request
# continues a session that already has prior turns, every outgoing prompt of
# the turn carries this ONE extra system line — heavy prior history (tool rows
# + a long answer) makes the small model drift back into free-form prose, so
# the contract is re-stated before each model call of the turn.
STANDING_PROTOCOL_REMINDER = (
    "Reminder: respond with exactly ONE JSON object \u2014 "
    '{"tool": "\u2026", "args": {...}} or {"answer": "\u2026", "urls": [...]} \u2014 '
    "nothing else."
)

_TRUNCATED_MARKER = (
    "\n...[tool result truncated \u2014 kept {kept} of {total} bytes; "
    "the full result is persisted in the session file]"
)
# Byte reserve for the truncation marker (marker digits change with sizes).
_TRUNCATION_RESERVE_BYTES = 160

_FALLBACK_SYSTEM_PROMPT = (
    "You are the NotifyDesk in-app agent. Reply with EXACTLY ONE JSON object per turn: "
    '{"tool": "<name>", "args": {...}} or {"answer": "<pt-br text>", "urls": [...]}. '
    "Valid tools: app.data, dashboard.create, server.serve, notify.test, native.call. "
    "The final 'answer' text is always in Brazilian Portuguese."
)


class AgentSessionError(ValueError):
    """Malformed session id (HTTP 400 at the API layer)."""


class AgentSessionNotFound(ValueError):
    """Unknown session id (HTTP 404 at the API layer)."""


# ---------------------------------------------------------------------------
# Session store (append-only JSONL under <config_dir>/data/agent_sessions)
# ---------------------------------------------------------------------------


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Live turn progress (in-memory side channel; additive, no disk writes)
# ---------------------------------------------------------------------------


def _raw_tail(text: str) -> str:
    """Tail ``RAW_PARTIAL_MAX_CHARS`` chars of the model's raw/thinking
    stream; longer texts keep the LAST chars prefixed with an ellipsis."""
    if len(text) > RAW_PARTIAL_MAX_CHARS:
        return "…" + text[-RAW_PARTIAL_MAX_CHARS:]
    return text


def _extract_tool_target(args: Any) -> str:
    """First string arg value in insertion order, skipping ``html`` and
    sliced to 24 chars — intentionally mirrors ``extractToolTarget``/toToolChip
    in desktop/src/viewModels/chatViewModel.ts so the LIVE running chip's
    target matches the completed chips' target. Empty string otherwise."""
    if not isinstance(args, dict):
        return ""
    for key, value in args.items():
        if key == "html":
            continue
        if isinstance(value, str):
            return value[:24]
    return ""


def _sanitize_step_for_progress(step: dict) -> dict:
    """Poll-sized copy of a persisted tool step: ``args`` stays a dict (so the
    frontend toToolChip target extraction works unchanged) while its ``html``
    VALUE collapses to the redacted short summary (no ~6 KB html echoes over
    the poll wire); every other key/value (and the
    ``role``/``tool``/``result``/``error``/``ts`` fields) stays verbatim."""
    sanitized = dict(step)
    args = step.get("args")
    if isinstance(args, dict):
        redacted_args = dict(args)
        html = redacted_args.get("html")
        if isinstance(html, str):
            redacted_args["html"] = summarize_tool_args({"html": html})
        sanitized["args"] = redacted_args
    return sanitized


class TurnProgressRegistry:
    """One live turn progress snapshot per agent runtime, polled over HTTP
    (``GET /api/agent/active-turn``) while ``POST /api/agent/turn`` runs on a
    sibling ThreadingHTTPServer thread.

    In-memory and transient (no SQLite/JSONL schema changes): every mutation
    and the ``snapshot()`` read are guarded by ``self._lock`` so a concurrent
    GET can never observe a torn ``completed_tools`` list, and ``snapshot()``
    returns deep copies (immutable from the caller's side). ``raw_partial``
    holds the model's LAST non-empty raw/thinking tail (capped to
    ``RAW_PARTIAL_MAX_CHARS``, ellipsis-prefixed) so polls between LLM calls
    still carry it. When a runtime provides no registry (None) every run_turn
    hook is a no-op — the turn contract is unchanged.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._session_id: Optional[str] = None
        self._phase: str = "idle"  # idle|thinking|retry|tool|serving|done
        self._attempt: int = 0
        self._started_at: Optional[str] = None
        self._current_tool: Optional[dict] = None
        self._tool_started_at: Optional[str] = None
        self._completed_tools: List[dict] = []
        self._raw_buffer: str = ""
        self._raw_partial: str = ""
        self._done: bool = False

    # ---- turn lifecycle (all run_turn milestones) ----

    def start_turn(self, session_id: str) -> None:
        """Reset every piece of state for a fresh turn; the turn opens in the
        ``thinking`` phase (attempt 1)."""
        with self._lock:
            self._session_id = session_id
            self._phase = "thinking"
            self._attempt = 1
            self._started_at = _utcnow_iso()
            self._current_tool = None
            self._tool_started_at = None
            self._completed_tools = []
            self._raw_buffer = ""
            self._raw_partial = ""
            self._done = False

    def begin_llm_call(self, session_id: str, phase: str = "thinking", attempt: int = 1) -> None:
        """One LLM call boundary (``thinking`` or ``retry``): the streamed
        buffer RESETS for the new call, but the last non-empty ``raw_partial``
        is KEPT until new chunks arrive."""
        with self._lock:
            self._session_id = session_id
            self._phase = phase
            self._attempt = attempt
            self._current_tool = None
            self._tool_started_at = None
            self._raw_buffer = ""

    def append_raw_chunk(self, chunk: str) -> None:
        """Streaming hook (Ollama NDJSON lines via _transport_post): append
        the chunk and refresh the tail-capped ``raw_partial``. Empty/no-op
        chunks are accepted gracefully."""
        if not chunk:
            return
        with self._lock:
            self._raw_buffer += chunk
            self._raw_partial = _raw_tail(self._raw_buffer)

    def set_raw_output(self, session_id: str, raw: str) -> None:
        """Non-streaming LLM return (llm_fn stubs, _mock_llm, final raw):
        cap-set ``raw_partial`` to the output tail. An empty raw leaves the
        previous tail in place."""
        with self._lock:
            if raw:
                self._session_id = session_id
                self._raw_partial = _raw_tail(raw)

    def begin_tool(self, session_id: str, tool_name: str, args: Any, phase: str = "tool") -> None:
        """Tool dispatch boundary (``tool``, or ``serving`` for the loop's
        auto-executed server.serve)."""
        with self._lock:
            self._session_id = session_id
            self._phase = phase
            self._current_tool = {"tool": tool_name, "target": _extract_tool_target(args)}
            self._tool_started_at = _utcnow_iso()

    def complete_tool(self, session_id: str, step: dict) -> None:
        """Tool finished: archive the sanitized step for the completed chips
        and clear the live running chip."""
        with self._lock:
            self._session_id = session_id
            self._completed_tools.append(_sanitize_step_for_progress(step))
            self._current_tool = None
            self._tool_started_at = None

    def finish_turn(self, session_id: str) -> None:
        """run_turn's finally: the turn is over (success, guard answer or
        exception) — no live tool may outlive it."""
        with self._lock:
            self._session_id = session_id
            self._phase = "done"
            self._current_tool = None
            self._tool_started_at = None
            self._done = True

    def reset_raw_buffer(self, session_id: Optional[str] = None) -> None:
        """Per-retry call from ``_ollama_chat``: the dead attempt's streamed
        buffer is dropped before the next attempt; the last non-empty
        ``raw_partial`` is preserved (same semantics as begin_llm_call)."""
        with self._lock:
            self._raw_buffer = ""

    def snapshot(self, session_id: Optional[str] = None) -> dict:
        """Deep-copied read-only view for the HTTP poll. When ``session_id``
        is requested and does not match the live turn, a truthful idle/done
        shape for the requested id is returned; with no param the live state
        is returned (a fresh registry reads session_id=None/phase=``idle``/
        done=False)."""
        with self._lock:
            if session_id is not None and session_id != self._session_id:
                return {
                    "session_id": session_id,
                    "phase": "idle",
                    "attempt": 0,
                    "started_at": None,
                    "current_tool": None,
                    "tool_started_at": None,
                    "completed_tools": [],
                    "raw_partial": "",
                    "done": True,
                }
            return {
                "session_id": self._session_id,
                "phase": self._phase,
                "attempt": self._attempt,
                "started_at": self._started_at,
                "current_tool": copy.deepcopy(self._current_tool),
                "tool_started_at": self._tool_started_at,
                "completed_tools": copy.deepcopy(self._completed_tools),
                "raw_partial": self._raw_partial,
                "done": self._done,
            }


class AgentSessionStore:
    """Append-only JSONL session persistence, resolved under the runtime's
    config dir so unit tests and smokes stay fully sandboxed."""

    def __init__(self, config_dir: str):
        self.config_dir = config_dir
        self.sessions_dir = os.path.join(config_dir, "data", "agent_sessions")

    @staticmethod
    def validate_session_id(session_id: Any) -> str:
        """^[A-Za-z0-9-]{1,64}$ — no traversal, no path separators."""
        if not isinstance(session_id, str) or not SESSION_ID_RE.match(session_id):
            raise AgentSessionError(f"invalid agent session id: {session_id!r}")
        return session_id

    @staticmethod
    def new_session_id() -> str:
        return str(uuid.uuid4())

    def _path(self, session_id: str) -> str:
        return os.path.join(self.sessions_dir, f"{session_id}.jsonl")

    def append(self, session_id: str, record: dict) -> None:
        """Append one JSONL record (mkdir on demand). The record's ``ts`` is
        stamped here so callers hold the exact persisted timestamp."""
        AgentSessionStore.validate_session_id(session_id)
        os.makedirs(self.sessions_dir, exist_ok=True)
        record.setdefault("ts", _utcnow_iso())
        line = json.dumps(record, ensure_ascii=False)
        with open(self._path(session_id), "a", encoding="utf-8") as f:
            f.write(line + "\n")

    def load(self, session_id: str) -> Optional[List[dict]]:
        """All records of one session (malformed lines skipped), or None when
        the session does not exist."""
        AgentSessionStore.validate_session_id(session_id)
        path = self._path(session_id)
        if not os.path.isfile(path):
            return None
        records: List[dict] = []
        with open(path, "r", encoding="utf-8") as f:
            for raw_line in f:
                raw_line = raw_line.strip()
                if not raw_line:
                    continue
                try:
                    record = json.loads(raw_line)
                except ValueError:
                    continue
                if isinstance(record, dict):
                    records.append(record)
        return records

    def scan(self) -> List[dict]:
        """[{session_id, updated_at, turns, preview}] newest-first.

        ``turns`` counts user messages (one per conversational turn request);
        ``preview`` reads only the first user line — never the whole file.
        """
        if not os.path.isdir(self.sessions_dir):
            return []
        rows: List[dict] = []
        for name in os.listdir(self.sessions_dir):
            if not name.endswith(".jsonl"):
                continue
            session_id = name[: -len(".jsonl")]
            path = os.path.join(self.sessions_dir, name)
            try:
                mtime = os.stat(path).st_mtime
            except OSError:  # pragma: no cover - raced cleanup
                continue
            turns, preview = self._summary(path)
            rows.append(
                {
                    "session_id": session_id,
                    "updated_at": datetime.fromtimestamp(mtime, timezone.utc).isoformat(),
                    "turns": turns,
                    "preview": preview,
                }
            )
        rows.sort(key=lambda row: row["updated_at"], reverse=True)
        return rows

    @staticmethod
    def _summary(path: str) -> Tuple[int, str]:
        turns = 0
        preview = ""
        with open(path, "r", encoding="utf-8") as f:
            for raw_line in f:
                raw_line = raw_line.strip()
                if not raw_line:
                    continue
                try:
                    record = json.loads(raw_line)
                except ValueError:
                    continue
                if isinstance(record, dict) and record.get("role") == "user":
                    turns += 1
                    if not preview:
                        preview = str(record.get("content", ""))
                        if len(preview) > 120:
                            preview = preview[:119] + "…"
        return turns, preview


# ---------------------------------------------------------------------------
# Prompt + context
# ---------------------------------------------------------------------------

_PROMPT_CACHE: Optional[str] = None


def load_system_prompt() -> str:
    """English system prompt template from agent_prompt.md (cached; minimal
    built-in fallback when the file is missing so the loop never hard-fails)."""
    global _PROMPT_CACHE
    if _PROMPT_CACHE is None:
        try:
            with open(PROMPT_PATH, "r", encoding="utf-8") as f:
                _PROMPT_CACHE = f.read()
        except OSError:
            logger.warning("agent_prompt.md not found; using minimal built-in prompt")
            _PROMPT_CACHE = _FALLBACK_SYSTEM_PROMPT
    return _PROMPT_CACHE


_WEEKDAYS_EN = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


def current_date_ground(now: Optional[datetime] = None) -> str:
    """English date-grounding line computed at prompt-build time from the
    LOCAL clock: ``Current local date: 2026-10-27 Tuesday (2026-10).``

    The template is formatted with a fixed English weekday table so the line
    stays English regardless of the system locale. The model has no other
    knowledge of "now" — this anchors relative-date tool args ("deste mês").
    """
    stamp = now if now is not None else datetime.now()
    weekday = _WEEKDAYS_EN[stamp.weekday()]
    return (
        f"Current local date: {stamp.strftime('%Y-%m-%d')} {weekday} "
        f"({stamp.strftime('%Y-%m')})."
    )


def grounded_system_prompt() -> str:
    """``load_system_prompt()`` with the current date line injected in the
    header (first line) — every LLM call sees the build-time date."""
    return f"{current_date_ground()}\n\n{load_system_prompt()}"


def truncate_tool_result_text(text: str, budget: int = TOOL_RESULT_MAX_BYTES) -> str:
    """Pre-truncate a stored tool result for the LLM context: tail-trimmed at
    ~4 KB with an explicit marker (the FULL result stays in the session jsonl)."""
    encoded = text.encode("utf-8")
    if len(encoded) <= budget:
        return text
    keep = max(budget - _TRUNCATION_RESERVE_BYTES, 0)
    piece = encoded[:keep].decode("utf-8", errors="ignore")
    marker = _TRUNCATED_MARKER.format(kept=len(piece.encode("utf-8")), total=len(encoded))
    return piece + marker


_RAW_OUTPUT_TRUNCATED_MARKER = (
    "\n...[raw model output truncated \u2014 kept {kept} of {total} bytes]"
)
_RAW_OUTPUT_RESERVE_BYTES = 128


def truncate_raw_model_output(text: str, budget: int = RAW_MODEL_OUTPUT_MAX_BYTES) -> str:
    """Persist-safe truncation of one RAW model output (capped at ~8 KB) so
    protocol-drift post-mortems always have the real bytes the model produced."""
    if not isinstance(text, str):
        text = "" if text is None else str(text)
    encoded = text.encode("utf-8")
    if len(encoded) <= budget:
        return text
    keep = max(budget - _RAW_OUTPUT_RESERVE_BYTES, 0)
    piece = encoded[:keep].decode("utf-8", errors="ignore")
    marker = _RAW_OUTPUT_TRUNCATED_MARKER.format(kept=len(piece.encode("utf-8")), total=len(encoded))
    return piece + marker


_ANSWER_TRUNCATED_MARKER = "…[answer truncated {omitted} chars in context]"


def truncate_prior_answer_text(text: Any) -> str:
    """Context-only truncation of a PRIOR assistant answer (round 3): answers
    longer than ~240 chars are reduced to their first ~200 chars plus an
    explicit marker; the FULL answer remains in the session jsonl on disk and
    in the UI. Short answers pass through verbatim; non-str values degrade to
    ""/str defensively (the loop itself only ever writes str answers)."""
    if not isinstance(text, str):
        text = "" if text is None else str(text)
    if len(text) <= PRIOR_ANSWER_INLINE_MAX_CHARS:
        return text
    omitted = len(text) - PRIOR_ANSWER_KEPT_CHARS
    return text[:PRIOR_ANSWER_KEPT_CHARS] + _ANSWER_TRUNCATED_MARKER.format(omitted=omitted)


def _stored_arg_marker(key: str, size: int) -> str:
    return f"<{key}: {size} bytes stored in the session file, not re-sent>"


def summarize_tool_args(args: Any) -> str:
    """Deterministic, short summary of a tool call's args for the LLM context.

    Large string/JSON values (e.g. the ~6 KB HTML echoed by dashboard.create)
    collapse to a size marker so the context never re-carries the full payload;
    small values stay verbatim. Returns "" when NOTHING was redacted — the
    caller then keeps rendering the original args JSON (small tools unchanged).
    """
    if not isinstance(args, dict) or not args:
        return ""
    redacted: Dict[str, Any] = {}
    changed = False
    for key, value in args.items():
        if isinstance(value, str):
            if len(value.encode("utf-8")) <= TOOL_ARG_INLINE_MAX_CHARS:
                redacted[str(key)] = value
            else:
                redacted[str(key)] = _stored_arg_marker(str(key), len(value.encode("utf-8")))
                changed = True
        else:
            rendered = json.dumps(value, ensure_ascii=False, default=str)
            if len(rendered.encode("utf-8")) <= TOOL_ARG_INLINE_MAX_CHARS:
                redacted[str(key)] = value
            else:
                redacted[str(key)] = _stored_arg_marker(str(key), len(rendered.encode("utf-8")))
                changed = True
    if not changed:
        return ""
    return json.dumps(redacted, ensure_ascii=False, default=str)


def _artifact_id_of(result: Any) -> Optional[str]:
    """Full artifact id when a tool result produced one, else None."""
    if isinstance(result, dict):
        artifact_id = result.get("artifact_id")
        if isinstance(artifact_id, str) and artifact_id:
            return artifact_id
    return None


def _served_url_of(result: Any) -> Optional[str]:
    """Loopback URL when a server.serve tool result produced one, else None."""
    if isinstance(result, dict):
        url = result.get("url")
        if isinstance(url, str) and url.strip():
            return url.strip()
    return None


def synthesize_dashboard_answer(create_result: Any, serve_result: Any) -> dict:
    """Pure synthesis of the final PT-BR answer for a COMPLETED dashboard
    pipeline (create + serve both succeeded) — used when the model drifts or
    fails to produce the final answer: the loop still delivers the artifact.

    Returns ``{"answer": <pt-br>, "urls": [<served url>]}``; a missing/
    malformed serve url degrades to ``urls: []`` with an explanatory answer.
    """
    url = _served_url_of(serve_result)
    if url:
        answer = f"Dashboard pronto: {url}. Abra na aba Chat para visualizar o painel."
        return {"answer": answer, "urls": [url]}
    return {
        "answer": "O dashboard foi criado, mas não consegui obter a URL local de exibição.",
        "urls": [],
    }


def continuation_reminder_for(records: List[dict]) -> Optional[str]:
    """Targeted recipe re-anchoring, keyed on the NEWEST meaningful record
    (``assistant_raw`` rows are skipped):

    - successful model-driven ``dashboard.create`` -> the server.serve
      continuation recipe with the real artifact id (kept as a defensive
      fallback; in the normal loop the serve step runs automatically);
    - successful loop-executed ``auto_serve`` step -> the finalize recipe
      with the real served URL: the model only wraps up with answer+urls.

    Otherwise None (the generic PROTOCOL_REMINDER path stays untouched for
    every other state).
    """
    record: Any = None
    for known_record in reversed(records):
        if isinstance(known_record, dict) and known_record.get("role") == "assistant_raw":
            continue
        record = known_record
        break
    if not isinstance(record, dict):
        return None
    if record.get("role") != "assistant" or "tool" not in record:
        return None
    if record.get("error") is not None:
        return None
    if record.get("tool") == "dashboard.create":
        artifact_id = _artifact_id_of(record.get("result"))
        if not artifact_id:
            return None
        return SERVE_CONTINUATION_TEMPLATE.replace("{artifact_id}", artifact_id)
    if record.get("tool") == "server.serve" and record.get("phase") == AUTO_SERVE_PHASE:
        serve_url = _served_url_of(record.get("result"))
        if not serve_url:
            return None
        return FINALIZE_CONTINUATION_TEMPLATE.replace("{serve_url}", serve_url)
    return None


def build_context(
    system_prompt: str,
    records: List[dict],
    window: int = SESSION_CONTEXT_MESSAGES,
) -> List[dict]:
    """System prompt + the last ``window`` rendered messages (1 record = 1
    message). Tool steps render as a compact transcript line so a strict
    one-JSON-per-turn model always sees its own exact output format."""

    rendered: List[dict] = []
    for record in records:
        role = record.get("role")
        if role == "user":
            rendered.append({"role": "user", "content": str(record.get("content", ""))})
        elif role == "assistant" and "answer" in record:
            # Context hygiene (round 3): PRIOR answers (guard errors and
            # successes alike) render with the answer text truncated when
            # long — the full text lives only in the session jsonl/UI.
            rendered.append(
                {
                    "role": "assistant",
                    "content": json.dumps(
                        {
                            "answer": truncate_prior_answer_text(record.get("answer", "")),
                            "urls": record.get("urls", []),
                        },
                        ensure_ascii=False,
                    ),
                }
            )
        elif role == "assistant" and "tool" in record:
            # Context hygiene: large tool args (the ~6 KB dashboard HTML) are
            # redacted to a short deterministic marker — only small args are
            # ever re-sent verbatim. The RESULT row stays intact (small) so
            # the artifact id remains discoverable.
            args_summary = summarize_tool_args(record.get("args"))
            call_view: Dict[str, Any] = {"tool": record.get("tool")}
            if args_summary:
                call_view["args_summary"] = args_summary
                artifact_id = _artifact_id_of(record.get("result"))
                if artifact_id:
                    call_view["artifact_hint"] = f"{artifact_id[:8]}\u2026html"
            else:
                call_view["args"] = record.get("args", {})
            lines = ["tool_call: " + json.dumps(call_view, ensure_ascii=False)]
            if record.get("error") is not None:
                lines.append("tool_error: " + json.dumps({"error": record.get("error")}, ensure_ascii=False))
            else:
                result = record.get("result")
                lines.append(
                    "tool_result: "
                    + truncate_tool_result_text(json.dumps(result, ensure_ascii=False, default=str))
                )
            rendered.append({"role": "assistant", "content": "\n".join(lines)})
        # unknown record shapes (future: raw bookkeeping rows) are skipped
    return [{"role": "system", "content": system_prompt}] + rendered[-window:]


# ---------------------------------------------------------------------------
# LLM functions
# ---------------------------------------------------------------------------


def default_llm_fn(runtime: Any) -> Callable[[List[dict]], str]:
    """Resolution order at run_turn level: injected llm_fn wins, then the
    NOTIFYBOT_SMOKE_AGENT_MOCK=1 scripted mock, then the production Ollama
    call below (single-call timeout = config.ollama.timeout, 30s default)."""
    if os.environ.get(MOCK_ENV, "") == "1":
        return _mock_llm
    return lambda messages: _ollama_chat(runtime, messages)


def _transport_post(runtime: Any, messages: List[dict]) -> str:
    """ONE POST to config.ollama.url ({model, stream:true, messages}) —
    same wire format as notifybot.classifier.LLMClassifier. Raises on any
    failure; ``_ollama_chat`` layers the transport retries on top of this
    seam (monkeypatched in tests).

    The reply is read by LINE ITERATION: Ollama streams newline-delimited
    JSON (one object per line; the last one carries ``"done": true``) while
    a non-streamed body is a single full-JSON line — the same loop handles
    both transparently. ``message.content`` is assembled and returned for
    parse_model_json; ``message.thinking`` (fallback ``message.content``) is
    fed into the runtime's live TurnProgressRegistry when present. urllib
    errors propagate unchanged so ``_ollama_chat``'s retry logic stays
    intact; every undecodable (non-UTF8), malformed-JSON, or non-object
    (scalar/array) line is skipped (the loop is resilient, strict per-line
    JSON is never fabricated from partial bytes).
    """
    from notifybot.api import read_config_strict  # lazy (api wires the loop)

    config = read_config_strict(runtime.config_path)
    ollama_cfg = config.get("ollama") or {}
    url = ollama_cfg.get("url") or "http://127.0.0.1:11434/api/chat"
    model = ollama_cfg.get("model") or "glm-5.3-flash:cloud"
    timeout = float(ollama_cfg.get("timeout", 30.0))
    payload = {"model": model, "stream": True, "messages": messages}
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    progress = getattr(runtime, "turn_progress", None)
    content = ""
    with urllib.request.urlopen(request, timeout=timeout) as resp:
        for raw_line in resp:
            try:
                line = raw_line.decode("utf-8") if isinstance(raw_line, bytes) else raw_line
                line = line.strip()
                if not line:
                    continue
                obj = json.loads(line)
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue  # undecodable/malformed line: skip it, never raise mid-stream
            if not isinstance(obj, dict):
                continue  # scalar/array JSON line: there is no message to read
            msg = obj.get("message") if isinstance(obj.get("message"), dict) else {}
            content += str(msg.get("content") or "")
            if progress is not None:
                progress.append_raw_chunk(str(msg.get("thinking") or msg.get("content") or ""))
    return content


def _transport_backoff(attempt: int) -> None:
    """Linear inter-attempt sleep before the next transport retry (2 s per
    attempt number). Module-level so tests can stub the wall-clock out."""
    time.sleep(max(0.0, OLLAMA_RETRY_BACKOFF_S * attempt))


def _is_transient_transport_error(exc: Exception) -> bool:
    """True for the mission's transient transport classes: URLError, HTTPError
    5xx, socket.timeout / TimeoutError. HTTPError 4xx is NOT retried (it is a
    deterministic request/model error, not a transport flake)."""
    if isinstance(exc, urllib.error.HTTPError):
        return 500 <= exc.code < 600
    if isinstance(exc, (urllib.error.URLError, socket.timeout, TimeoutError)):
        return True
    return False


def _ollama_chat(runtime: Any, messages: List[dict]) -> str:
    """Production LLM call with per-call transport retries: up to 2 extra
    attempts with 2 s linear backoff on transient transport errors
    (URLError / HTTPError 5xx / socket.timeout / TimeoutError). The
    per-attempt timeout stays config.ollama.timeout (30 s default). After
    the retries are exhausted the exception propagates and run_turn converts
    it into the unchanged graceful PT-BR guard answer."""
    attempts = 1 + OLLAMA_TRANSPORT_RETRIES
    last_exc: Optional[Exception] = None
    for attempt in range(attempts):
        try:
            return _transport_post(runtime, messages)
        except (urllib.error.URLError, socket.timeout, TimeoutError) as e:
            if not _is_transient_transport_error(e):
                raise
            last_exc = e
            if attempt + 1 < attempts:
                logger.warning(
                    "Agent LLM transport failure (attempt %d/%d): %s", attempt + 1, attempts, e
                )
                # Retry hygiene (placement: at the TOP of the retry branch, so
                # even a stubbed _transport_backoff in tests still sees the
                # reset): the next attempt re-streams from scratch, so drop
                # the dead attempt's buffer while keeping the last non-empty
                # raw_partial (same buffer semantics as begin_llm_call).
                progress = getattr(runtime, "turn_progress", None)
                if progress is not None:
                    progress.reset_raw_buffer()
                _transport_backoff(attempt + 1)
    assert last_exc is not None  # loop always sets it before exhausting
    raise last_exc


MOCK_DASHBOARD_TITLE = "Agent Health Overview"
MOCK_DASHBOARD_DATA_SOURCE = "app.data:health"
MOCK_REMINDER_LIST = "Leituras"
MOCK_REMINDER_TITLE = "Ler o relatório do projeto X"


def _mock_dashboard_html() -> str:
    """Deterministic self-contained artifact (script-free by construction)."""
    return (
        "<!DOCTYPE html>"
        "<html><head><title>Agent Health Overview</title>"
        "<style>body{font-family:-apple-system,sans-serif;margin:24px;color:#1b2430}"
        "h1{color:#1a7f64;font-size:22px}"
        ".stat{border:1px solid #d7dde5;border-radius:8px;padding:10px 14px;margin:8px 0}"
        "</style></head><body>"
        f"<h1>{MOCK_DASHBOARD_TITLE}</h1>"
        "<p>Self-contained dashboard generated by the NotifyDesk agent (mock mode).</p>"
        f"<div class='stat'>Data source: {MOCK_DASHBOARD_DATA_SOURCE}</div>"
        "</body></html>"
    )


def _mock_llm(messages: List[dict]) -> str:
    """Deterministic scripted LLM for the API smoke (TEST/SMOKE-ONLY)."""
    user_text = ""
    for message in reversed(messages):
        if message.get("role") != "user":
            continue
        # Reminder/continuation messages are loop bookkeeping, not the request.
        if str(message.get("content", "")).startswith(REMINDER_PREFIX):
            continue
        user_text = str(message.get("content", ""))
        break

    tool_calls = 0
    last_call_name: Optional[str] = None
    last_call_args: Dict[str, Any] = {}
    last_result: dict = {}
    for message in messages:
        if message.get("role") != "assistant":
            continue
        content = str(message.get("content", ""))
        if content.startswith("tool_call:"):
            tool_calls += 1
            try:
                call = json.loads(content.splitlines()[0][len("tool_call:") :])
                if isinstance(call, dict):
                    last_call_name = call.get("tool")
                    args_value = call.get("args")
                    last_call_args = args_value if isinstance(args_value, dict) else {}
            except (ValueError, IndexError):
                pass
        for line in content.splitlines():
            if line.startswith("tool_result:"):
                try:
                    parsed_result = json.loads(line[len("tool_result:") :])
                    if isinstance(parsed_result, dict):
                        last_result = parsed_result
                except ValueError:
                    pass
            elif line.startswith("tool_error:"):
                last_result = {}

    text = user_text.lower()
    answer = lambda body_text, url_items: json.dumps({"answer": body_text, "urls": url_items}, ensure_ascii=False)
    tool = lambda name, args: json.dumps({"tool": name, "args": args}, ensure_ascii=False)

    notification_words = ("test notif", "notificação de teste", "teste de notificação", "testar notific")
    dashboard_words = ("dashboard", "painel", "relatório", "relatorio")
    reminder_words = ("lembrete", "reminder", "nota", "note")

    if any(word in text for word in reminder_words):
        if last_call_name != "native.call":
            return tool(
                "native.call",
                {
                    "domain": "reminders",
                    "action": "add",
                    "params": {"list": MOCK_REMINDER_LIST, "title": MOCK_REMINDER_TITLE},
                },
            )
        last_action = str((last_call_args or {}).get("action", ""))
        if last_action == "add":
            return tool(
                "native.call",
                {
                    "domain": "reminders",
                    "action": "list_reminders",
                    "params": {"list": MOCK_REMINDER_LIST},
                },
            )
        items = last_result.get("reminders")
        count = len(items) if isinstance(items, list) else 0
        return answer(
            f"Criei o lembrete {MOCK_REMINDER_TITLE!r} na lista {MOCK_REMINDER_LIST!r} — "
            f"agora há {count} lembrete(s) pendente(s) nela.",
            [],
        )

    if any(word in text for word in notification_words):
        if last_call_name != "notify.test":
            return tool("notify.test", {})
        return answer("Enviei 3 notificações de teste para o macOS —elas devem aparecer na tela agora.", [])

    if any(word in text for word in dashboard_words):
        if tool_calls == 0:
            return tool("app.data", {"dataset": "health"})
        if tool_calls == 1:
            return tool(
                "dashboard.create",
                {
                    "title": MOCK_DASHBOARD_TITLE,
                    "html": _mock_dashboard_html(),
                    "data_source": MOCK_DASHBOARD_DATA_SOURCE,
                },
            )
        if tool_calls == 2:
            artifact_id = last_result.get("artifact_id")
            if isinstance(artifact_id, str) and artifact_id:
                return tool("server.serve", {"artifact_id": artifact_id})
            return answer("Criei o dashboard, mas não consegui localizar o artefato para servir.", [])
        url = last_result.get("url")
        if isinstance(url, str) and url:
            return answer(
                f"Aqui está o dashboard {MOCK_DASHBOARD_TITLE!r} com os dados do bot, pronto na aba Chat.",
                [url],
            )
        return answer("O dashboard foi criado, mas não consegui gerar a URL de exibição.", [])

    return answer(
        "Ok! Posso montar um dashboard com os dados do bot (health, eventos, financeiro) "
        "ou enviar notificações de teste. O que você prefere?",
        [],
    )


# ---------------------------------------------------------------------------
# Strict protocol parsing
# ---------------------------------------------------------------------------


def parse_model_json(text: Any) -> Optional[dict]:
    """Tolerant strict-protocol parse: exactly one JSON object per model turn
    — either {"tool": str, "args": dict} or {"answer": str, "urls": [str]}.

    Reuses notifybot.classifier.clean_json_text (fence stripping + first
    {...} block). Returns None on any unparsable/protocol-invalid output —
    run_turn turns that into one protocol-reminder retry, then a PT-BR error
    answer.
    """
    if not isinstance(text, str) or not text.strip():
        return None
    try:
        parsed = json.loads(clean_json_text(text))
    except (ValueError, TypeError):
        return None
    if not isinstance(parsed, dict):
        return None

    tool = parsed.get("tool")
    if tool is not None:
        if not isinstance(tool, str) or not tool.strip():
            return None
        args = parsed.get("args")
        return {"tool": tool.strip(), "args": args if isinstance(args, dict) else {}}

    answer = parsed.get("answer")
    if isinstance(answer, str) and answer.strip():
        urls_raw = parsed.get("urls", [])
        urls: List[str] = []
        if isinstance(urls_raw, list):
            urls = [url for url in urls_raw if isinstance(url, str)]
        return {"answer": answer, "urls": urls}
    return None


# ---------------------------------------------------------------------------
# The turn loop
# ---------------------------------------------------------------------------


def _tool_registry_for(runtime: Any) -> Optional[Any]:
    """BotApiRuntime owns the ToolRegistry singleton (api.py wiring)."""
    return getattr(runtime, "tool_registry", None)


def _completed_pipeline_results(turn_steps: List[dict]) -> Tuple[Optional[dict], Optional[dict]]:
    """Results of the LAST successful ``dashboard.create`` and a successful
    ``server.serve`` executed AFTER it (model-driven or auto) within one
    turn — i.e. a completed create->serve pipeline; else (None, None)."""
    create_index: Optional[int] = None
    for index, step in enumerate(turn_steps):
        if (
            isinstance(step, dict)
            and step.get("role") == "assistant"
            and step.get("tool") == "dashboard.create"
            and step.get("error") is None
        ):
            create_index = index
    serve_index: Optional[int] = None
    if create_index is not None:
        for index in range(create_index + 1, len(turn_steps)):
            step = turn_steps[index]
            if (
                isinstance(step, dict)
                and step.get("role") == "assistant"
                and step.get("tool") == "server.serve"
                and step.get("error") is None
            ):
                serve_index = index
    if create_index is None or serve_index is None:
        return None, None
    create_result = turn_steps[create_index].get("result")
    serve_result = turn_steps[serve_index].get("result")
    if not isinstance(create_result, dict) or not isinstance(serve_result, dict):
        return None, None
    return create_result, serve_result


def run_turn(
    runtime: Any,
    session_id_or_none: Optional[str],
    message: str,
    llm_fn: Optional[Callable[[List[dict]], str]] = None,
) -> dict:
    """Run one synchronous agent turn; returns
    ``{session_id, turn: [{role, tool, args, result?|error?, ts}...],
    answer, urls[]}`` (additively ``phase: 'auto_complete'`` when the final
    answer was loop-synthesized).

    Failure paths (LLM error, unparsable-with-retry, max turns, budget
    exceeded) all produce PT-BR error answers with the session preserved on
    disk — the loop never raises for content/model failures. When a
    create->serve pipeline already completed in the turn and the model fails
    to produce the answer anyway, the guard answer is REPLACED by the
    synthesized final answer (``auto_complete``) so the pipeline completes.
    """
    store = AgentSessionStore(runtime.config_dir)
    if not isinstance(message, str) or message.strip() == "":
        raise ValueError("agent message must be a non-empty string")
    message = message.strip()
    if len(message.encode("utf-8")) > AGENT_MESSAGE_MAX_BYTES:
        raise ValueError(f"agent message is too large (limit {AGENT_MESSAGE_MAX_BYTES} bytes)")

    if session_id_or_none is not None:
        session_id = store.validate_session_id(session_id_or_none)
        records = store.load(session_id)
        if records is None:
            raise AgentSessionNotFound(f"agent session not found: {session_id}")
    else:
        session_id = store.new_session_id()
        records = []

    # Standing reminder decision point (round 3): "non-empty history" means
    # this request CONTINUES a session with prior turns — records exist
    # BEFORE this turn's user message is appended below. Fresh sessions keep
    # the plain grounded prompt.
    had_prior_history = len(records) > 0

    # Live turn progress (additive, in-memory; without a registry on the
    # runtime every hook below is inert). Resolved BEFORE the wrapped region
    # so the finally below always references a bound name — a NameError
    # inside a finally would mask the original exception otherwise.
    progress = getattr(runtime, "turn_progress", None)

    user_record = {"role": "user", "content": message}
    store.append(session_id, user_record)
    records.append(user_record)
    if progress is not None:
        progress.start_turn(session_id)

    # Every code path below (budget break, llm_down break, unparsable,
    # max_turns, synthesis, return) exits through this finally, so the live
    # turn ALWAYS finalizes with done=True — including the raise paths of
    # resolve_llm/registry resolution.
    try:
        resolve_llm = llm_fn if llm_fn is not None else default_llm_fn(runtime)
        system_prompt = grounded_system_prompt()
        if had_prior_history:
            system_prompt = system_prompt + "\n" + STANDING_PROTOCOL_REMINDER
        registry = _tool_registry_for(runtime)

        started = time.monotonic()
        turn_steps: List[dict] = []
        answer_text: Optional[str] = None
        urls: List[str] = []
        model_answered = False

        budget_answer = (
            f"Desculpe, a operação excedeu o tempo limite interno ({int(TURN_BUDGET_S)}s) "
            "e foi interrompida no meio. Tente um pedido mais simples ou repita o pedido."
        )
        llm_down_answer = (
            "Desculpe, não consegui falar com o modelo de IA agora. Verifique se o Ollama "
            "está rodando e tente novamente em instantes."
        )

        for turn_index in range(MAX_TOOL_TURNS):
            if time.monotonic() - started > TURN_BUDGET_S:
                answer_text = budget_answer
                break

            continuation = continuation_reminder_for(records)
            extra_messages: List[dict] = []
            if continuation is not None:
                # Recipe re-anchoring: right after a successful dashboard.create
                # the next prompt carries the exact server.serve continuation.
                extra_messages.append({"role": "user", "content": continuation})

            if progress is not None:
                progress.begin_llm_call(session_id, phase="thinking", attempt=1)
            try:
                raw = resolve_llm(build_context(system_prompt, records) + extra_messages)
            except Exception as e:  # noqa: BLE001 - graceful LLM failure, never a 500
                logger.warning("Agent LLM call failed: %s", e)
                answer_text = llm_down_answer
                break

            if progress is not None:
                progress.set_raw_output(session_id, str(raw))

            # Observability: persist the RAW output of every LLM call before
            # parsing (phase-tagged; cap ~8 KB) so drift post-mortems use real data.
            store.append(
                session_id,
                {
                    "role": "assistant_raw",
                    "phase": "attempt",
                    "raw_model_output": truncate_raw_model_output(str(raw)),
                },
            )

            parsed = parse_model_json(raw)
            if parsed is None:
                # One retry, not persisted to the session. When the drift happened
                # right after dashboard.create, the retry re-anchors with the
                # targeted continuation recipe instead of the generic reminder.
                retry_tail = continuation if continuation is not None else PROTOCOL_REMINDER
                if progress is not None:
                    progress.begin_llm_call(session_id, phase="retry", attempt=1)
                try:
                    retry_raw = resolve_llm(
                        build_context(system_prompt, records) + [{"role": "user", "content": retry_tail}]
                    )
                except Exception as e:  # noqa: BLE001
                    logger.warning("Agent LLM protocol-retry failed: %s", e)
                    answer_text = llm_down_answer
                    break
                if progress is not None:
                    progress.set_raw_output(session_id, str(retry_raw))
                store.append(
                    session_id,
                    {
                        "role": "assistant_raw",
                        "phase": "protocol_retry",
                        "raw_model_output": truncate_raw_model_output(str(retry_raw)),
                    },
                )
                parsed = parse_model_json(retry_raw)
                if parsed is None:
                    answer_text = (
                        "Desculpe, o modelo não respondeu no formato JSON esperado duas vezes "
                        "seguidas. A conversa continua salva — tente reformular o pedido."
                    )
                    break

            if "tool" in parsed:
                if time.monotonic() - started > TURN_BUDGET_S:
                    answer_text = budget_answer
                    break
                tool_name = parsed["tool"]
                tool_args = parsed.get("args", {})
                if progress is not None:
                    progress.begin_tool(session_id, tool_name, tool_args, phase="tool")
                error: Optional[str] = None
                result: Optional[dict] = None
                if registry is None:
                    error = "tool registry unavailable"
                else:
                    dispatched = registry.dispatch(
                        tool_name, tool_args, ctx={"turn_index": turn_index, "session_id": session_id}
                    )
                    if isinstance(dispatched, dict) and "error" in dispatched:
                        error = str(dispatched["error"])
                    elif isinstance(dispatched, dict):
                        result = dispatched
                    else:
                        error = f"invalid tool result: {dispatched!r}"
                step: dict = {"role": "assistant", "tool": tool_name, "args": tool_args}
                if error is None:
                    step["result"] = result
                else:
                    step["error"] = error
                store.append(session_id, step)  # append stamps the shared ts
                if progress is not None:
                    progress.complete_tool(session_id, step)
                records.append(step)
                turn_steps.append(step)

                # Deterministic recipe completion: when the MODEL-driven
                # dashboard.create succeeded, the loop itself executes the
                # server.serve step — no model call, and it does NOT consume a
                # MAX_TOOL_TURNS slot (the cap counts model tool turns only).
                # Budget guard still applies to the auto step.
                artifact_id = (
                    _artifact_id_of(result)
                    if (tool_name == "dashboard.create" and error is None and result is not None)
                    else None
                )
                if artifact_id is None:
                    continue

                if time.monotonic() - started > TURN_BUDGET_S:
                    answer_text = budget_answer
                    break

                serve_args = {"action": "ensure", "artifact_id": artifact_id}
                if progress is not None:
                    progress.begin_tool(session_id, "server.serve", serve_args, phase="serving")
                serve_error: Optional[str] = None
                serve_result: Optional[dict] = None
                if registry is None:
                    serve_error = "tool registry unavailable"
                else:
                    served = registry.dispatch("server.serve", serve_args, ctx={"turn_index": turn_index, "session_id": session_id})
                    if isinstance(served, dict) and "error" in served:
                        serve_error = str(served["error"])
                    elif isinstance(served, dict):
                        serve_result = served
                    else:
                        serve_error = f"invalid tool result: {served!r}"
                auto_step: dict = {
                    "role": "assistant",
                    "tool": "server.serve",
                    "args": serve_args,
                    "phase": AUTO_SERVE_PHASE,
                }
                if serve_error is None:
                    auto_step["result"] = serve_result
                else:
                    auto_step["error"] = serve_error
                store.append(session_id, auto_step)
                if progress is not None:
                    progress.complete_tool(session_id, auto_step)
                records.append(auto_step)
                turn_steps.append(auto_step)
                continue

            answer_text = parsed["answer"]
            urls = list(parsed.get("urls", []))
            model_answered = True
            break

        if answer_text is None:
            answer_text = (
                f"Desculpe, atingi o limite de {MAX_TOOL_TURNS} passos de ferramenta nesta resposta "
                "sem concluir a tarefa. Peça de novo e eu continuo a partir daqui."
            )

        answer_record = {"role": "assistant", "answer": answer_text, "urls": urls}
        if not model_answered:
            # Deterministic completion: when create->serve already succeeded but
            # the loop exits WITHOUT a model answer (drift/LLM-down/turn cap/
            # budget), the guard answer is replaced by the synthesized final
            # answer carrying the served URL — the user-facing pipeline
            # completes; the record/turn is marked ``auto_complete``.
            create_result, serve_result = _completed_pipeline_results(turn_steps)
            if create_result is not None and serve_result is not None:
                synthesized = synthesize_dashboard_answer(create_result, serve_result)
                answer_record["answer"] = answer_text = synthesized["answer"]
                answer_record["urls"] = urls = list(synthesized["urls"])
                answer_record["phase"] = AUTO_COMPLETE_PHASE
        store.append(session_id, answer_record)

        payload = {
            "session_id": session_id,
            "turn": turn_steps,
            "answer": answer_text,
            "urls": urls,
        }
        if answer_record.get("phase") == AUTO_COMPLETE_PHASE:
            payload["phase"] = AUTO_COMPLETE_PHASE
        return payload
    finally:
        if progress is not None:
            progress.finish_turn(session_id)