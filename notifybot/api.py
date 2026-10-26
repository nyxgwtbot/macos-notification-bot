"""Local JSON API for the Desktop UI (`python3 bot.py api`).

Serves the endpoint rows of the Electron desktop plan over a stdlib
``ThreadingHTTPServer`` bound to loopback only:

1. GET  /api/health                 -> watermark/log/finance/ollama overview
2. GET  /api/events                 -> JSONL tail with `since` cursor
3. GET  /api/config                 -> config with secrets masked
4. POST /api/config                 -> schema-lite validation + atomic save
5. GET  /api/finance/summary        -> monthly totals (read-only ledger)
6. GET  /api/finance/transactions   -> monthly ledger rows (read-only)
7. GET  /api/finance/report         -> report status (no generation)
8. POST /api/finance/report         -> builds reports/<YYYY-MM>.html
9. POST /api/test-notify            -> reuses cli.trigger_test_notifications()
10. anything else                   -> 404 / 405 JSON
Plus one support endpoint for the Logs screen (raw text tail with malformed
lines flagged, needed to render them dimmed): GET /api/logs/tail?lines=N.

Hard criteria honored here:
- binds 127.0.0.1 by default and refuses non-loopback hosts;
- default port 4769 (overridable via --port / NOTIFYBOT_API_PORT);
- every response carries Access-Control-Allow-Origin: * (loopback-only
  binding makes this safe; it also enables the vite browser-dev path);
- api mode never spawns/manages the daemon and never touches db2:
  no `notifybot.reader`/`parser` imports, no Notification Center access;
- finance reads use mode=ro SQLite connections with busy_timeout=3000 and
  never create the schema (missing db -> db_exists:false zeroed responses);
- config writes are atomic (tmp file + os.replace, mirroring state.py).

Paths (log_file/state_file/ledger_db/reports) are resolved relative to the
config file's directory, so a custom `--config <tmp>/config.json` keeps the
smoke tests fully sandboxed while the default `config.json` (repo root)
behaves exactly like the daemon.
"""

import contextlib
import io
import json
import logging
import os
import sqlite3
import sys
import time
import urllib.parse
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from notifybot import __version__
from notifybot.finance import LEDGER_SCHEMA, FinanceLedger, previous_month
from notifybot.report import FinanceReporter, validate_month

logger = logging.getLogger("notification_bot")

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 4769

EVENT_KIND_ALL = "all"
EVENT_KIND_NOTIFICATION = "notification"
EVENT_KIND_STARTUP = "startup"
VALID_EVENT_KINDS = frozenset({EVENT_KIND_ALL, EVENT_KIND_NOTIFICATION, EVENT_KIND_STARTUP})
DEFAULT_EVENT_LIMIT = 100
MIN_EVENT_LIMIT = 1
MAX_EVENT_LIMIT = 500
DEFAULT_LOG_TAIL_LINES = 200
MIN_LOG_TAIL_LINES = 1
MAX_LOG_TAIL_LINES = 1000

# Back-seek tail window: never read more than this many bytes of events.jsonl
# per request (the response degrades to "recent window" on huge files).
MAX_EVENT_TAIL_BYTES = 1024 * 1024
MAX_JSON_BODY_BYTES = 1024 * 1024

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

SECRET_MASK = "********"

# Rule condition keys actually evaluated by router.RuleRouter._matches:
# app/title/subtitle/body come from the notification, category/urgency/summary
# from the classification. Anything else can never match -> rejected on save.
RULE_CONDITION_FIELDS = frozenset({"app", "title", "subtitle", "body", "category", "urgency", "summary"})

ACTION_TYPES = frozenset({"builtin", "command"})

CORS_ALLOW_METHODS = "GET, POST, OPTIONS"
CORS_ALLOW_HEADERS = "Content-Type"
CORS_MAX_AGE = "600"


class ApiError(Exception):
    """Raised by endpoint handlers; carries an HTTP status + JSON error body."""

    def __init__(self, status: int, error: str, details: Optional[List[str]] = None):
        super().__init__(error)
        self.status = int(status)
        self.error = error
        self.details = list(details or [])
        self.body: Dict[str, Any] = {"error": self.error}
        if self.details:
            self.body["details"] = self.details


def _is_number(value: Any) -> bool:
    """True for int/float but NOT bool (bool is an int subclass in Python)."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _dec_to_float(value: Any) -> Optional[float]:
    """Serialize Decimal amounts consistently as float (None passthrough)."""
    if value is None:
        return None
    try:
        return float(Decimal(str(value)))
    except Exception:
        return 0.0


def _is_nonempty_str(value: Any) -> bool:
    return isinstance(value, str) and value.strip() != ""


def _is_str_list(value: Any, allow_empty: bool = True) -> bool:
    if not isinstance(value, list):
        return False
    for item in value:
        if not isinstance(item, str):
            return False
        if not allow_empty and item.strip() == "":
            return False
    return True


# ---------------------------------------------------------------------------
# Config helpers (get / mask / validate / atomic save)
# ---------------------------------------------------------------------------


def read_config_strict(config_path: str) -> dict:
    """Read config.json for the API: missing file -> cli.load_config defaults,
    unreadable/unparsable -> ApiError 500 (never write on top of a file we
    cannot parse)."""
    from notifybot.cli import load_config

    if not os.path.exists(config_path):
        return load_config(config_path)

    try:
        with open(config_path, "r", encoding="utf-8") as f:
            loaded = json.load(f)
        if not isinstance(loaded, dict):
            raise ValueError("config deve ser um objeto JSON")
    except Exception as e:
        raise ApiError(500, f"config.json ilegível: {e}") from e

    # Pure defaults (probe path never exists as a config file).
    probe = os.path.join(os.path.dirname(os.path.abspath(config_path)) or ".", ".config-not-found-probe-json")
    config = load_config(probe)
    if os.path.basename(config_path) == os.path.basename(probe):  # pragma: no cover - defensive
        raise ApiError(500, "config probe colidiu com o arquivo real")
    config.update(loaded)
    return config


def mask_config_secrets(config: dict) -> dict:
    """Deep-copy the config and mask non-empty telegram token/chat_id.

    The API never exfiltrates stored secrets over HTTP.
    """
    masked = json.loads(json.dumps(config, ensure_ascii=False))
    telegram = masked.get("telegram")
    if isinstance(telegram, dict):
        if _is_nonempty_str(telegram.get("token")):
            telegram["token"] = SECRET_MASK
        if _is_nonempty_str(telegram.get("chat_id")):
            telegram["chat_id"] = SECRET_MASK
    return masked


def validate_config_payload(body: Any) -> List[str]:
    """Schema-lite validation for POST /api/config (mirrors the config
    semantics consumed by cli.load_config / router / actions / finance).

    Returns a list of violation strings (empty list == valid).
    """
    details: List[str] = []
    if not isinstance(body, dict):
        return ["corpo deve ser um objeto JSON (config completa)"]

    for section in ("ollama", "telegram", "router", "finance"):
        if not isinstance(body.get(section), dict):
            details.append(f"seção '{section}' deve ser um objeto")
    for section in ("actions", "rules"):
        if not isinstance(body.get(section), list):
            details.append(f"seção '{section}' deve ser uma lista")

    interval = body.get("poll_interval")
    if not _is_number(interval) or float(interval) <= 0:
        details.append("'poll_interval' deve ser um número > 0")

    ollama = body.get("ollama")
    if isinstance(ollama, dict):
        if not isinstance(ollama.get("url"), str) or ollama.get("url", "").strip() == "":
            details.append("'ollama.url' deve ser uma string não vazia")
        if not isinstance(ollama.get("model"), str) or ollama.get("model", "").strip() == "":
            details.append("'ollama.model' deve ser uma string não vazia")
        timeout = ollama.get("timeout")
        if not _is_number(timeout) or float(timeout) <= 0:
            details.append("'ollama.timeout' deve ser um número > 0")

    telegram = body.get("telegram")
    if isinstance(telegram, dict):
        token = telegram.get("token")
        chat_id = telegram.get("chat_id")
        if not isinstance(token, str) or token != "":
            details.append(
                "'telegram.token' deve ser vazio — tokens são env-only "
                "(TELEGRAM_BOT_TOKEN / TG_BOT_TOKEN)"
            )
        if not isinstance(chat_id, str) or chat_id != "":
            details.append(
                "'telegram.chat_id' deve ser vazio — chat é env-only "
                "(TELEGRAM_ALLOWED_USERS / TG_CHAT_ID)"
            )

    router_cfg = body.get("router")
    if isinstance(router_cfg, dict):
        min_conf = router_cfg.get("min_confidence")
        if not _is_number(min_conf) or not (0.0 <= float(min_conf) <= 1.0):
            details.append("'router.min_confidence' deve ser um número entre 0 e 1")
        router_timeout = router_cfg.get("timeout")
        if not _is_number(router_timeout) or float(router_timeout) <= 0:
            details.append("'router.timeout' deve ser um número > 0")

    actions = body.get("actions")
    if isinstance(actions, list):
        seen_ids: set = set()
        for index, action in enumerate(actions):
            if not isinstance(action, dict):
                details.append(f"actions[{index}] deve ser um objeto")
                continue
            action_id = action.get("id")
            if not _is_nonempty_str(action_id):
                details.append(f"actions[{index}].id deve ser uma string não vazia")
            elif action_id in seen_ids:
                details.append(f"id de ação duplicado: '{action_id}'")
            else:
                seen_ids.add(action_id)
            if action.get("type") not in ACTION_TYPES:
                details.append(
                    f"actions[{index}].type deve ser 'builtin' ou 'command' "
                    f"(recebido: {action.get('type')!r})"
                )
            if not isinstance(action.get("enabled"), bool):
                details.append(f"actions[{index}].enabled deve ser booleano")
            if not isinstance(action.get("match_hint"), str):
                details.append(f"actions[{index}].match_hint deve ser string")
            if not isinstance(action.get("config"), dict):
                details.append(f"actions[{index}].config deve ser um objeto")

    rules = body.get("rules")
    if isinstance(rules, list):
        for index, rule in enumerate(rules):
            rule_name = rule.get("name", index) if isinstance(rule, dict) else index
            if not isinstance(rule, dict):
                details.append(f"rules[{index}] deve ser um objeto")
                continue
            if not isinstance(rule.get("name"), str):
                details.append(f"rules[{index}].name deve ser string")
            condition = rule.get("condition")
            if not isinstance(condition, dict):
                details.append(f"regra '{rule_name}': condition deve ser um objeto")
            else:
                for cond_key, cond_value in condition.items():
                    if cond_key not in RULE_CONDITION_FIELDS:
                        allowed = ", ".join(sorted(RULE_CONDITION_FIELDS))
                        details.append(
                            f"regra '{rule_name}': campo de condição desconhecido "
                            f"'{cond_key}' (permitidos: {allowed})"
                        )
                    elif not (isinstance(cond_value, str) or _is_str_list(cond_value, allow_empty=False)):
                        details.append(
                            f"regra '{rule_name}': valores de '{cond_key}' devem ser "
                            "string ou lista de strings"
                        )
            rule_actions = rule.get("actions")
            if (
                not isinstance(rule_actions, list)
                or len(rule_actions) == 0
                or not _is_str_list(rule_actions, allow_empty=False)
            ):
                details.append(f"regra '{rule_name}': actions deve ser uma lista de strings não vazias")

    finance = body.get("finance")
    if isinstance(finance, dict):
        if not isinstance(finance.get("enabled"), bool):
            details.append("'finance.enabled' deve ser booleano")
        if not isinstance(finance.get("ledger_db"), str):
            details.append("'finance.ledger_db' deve ser string")
        if not _is_str_list(finance.get("bank_apps")):
            details.append("'finance.bank_apps' deve ser uma lista de strings")
        if not isinstance(finance.get("telegram_summary"), bool):
            details.append("'finance.telegram_summary' deve ser booleano")
        if not _is_str_list(finance.get("categories"), allow_empty=False):
            details.append("'finance.categories' deve ser uma lista de strings não vazias")

    return details


def save_config_atomic(config_path: str, data: dict) -> None:
    """Atomic config write: tmp file (same dir) + fsync + os.replace,
    mirroring notifybot/state.py StateManager.save. Never leaves a torn file."""
    directory = os.path.dirname(os.path.abspath(config_path))
    os.makedirs(directory, exist_ok=True)
    tmp_path = f"{config_path}.tmp.{os.getpid()}"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, config_path)
    except Exception:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass
        raise


# ---------------------------------------------------------------------------
# Events JSONL tail reader (back-seeking, malformed-line tolerant)
# ---------------------------------------------------------------------------


def _parse_event_line(raw: str) -> Optional[dict]:
    """Parse one JSONL line; malformed lines are skipped (mirrors
    scripts/smoke_e2e.py read_events tolerance)."""
    raw = raw.strip()
    if not raw:
        return None
    try:
        event = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(event, dict):
        return None
    return event


def read_events_tail(
    log_path: str,
    limit: int,
    since: Optional[int],
    kind: str,
    max_bytes: int = MAX_EVENT_TAIL_BYTES,
) -> Tuple[List[dict], Optional[int]]:
    """Read the tail of the events JSONL by back-seeking (never the whole file).

    Returns (events, latest_rec_id):
    - events: raw JSONL dicts, oldest-first, first `limit` items that pass the
      since/kind filters (callers page forward by raising their `since`);
    - latest_rec_id: highest rec_id observed in the scanned window (or None).
    """
    if not os.path.exists(log_path):
        return [], None

    try:
        size = os.path.getsize(log_path)
        start = max(0, size - max_bytes)
        with open(log_path, "rb") as f:
            f.seek(start)
            data = f.read()
    except OSError as e:
        logger.warning("Falha ao ler %s: %s", log_path, e)
        return [], None

    text = data.decode("utf-8", errors="replace")
    if start > 0:
        if "\n" not in text:
            return [], None
        text = text.split("\n", 1)[1]  # skip the cut-off partial first line

    parsed: List[dict] = []
    latest_rec_id: Optional[int] = None
    for raw_line in text.splitlines():
        event = _parse_event_line(raw_line)
        if event is None:
            continue
        parsed.append(event)
        if event.get("event") == EVENT_KIND_NOTIFICATION:
            rec_id = event.get("rec_id")
            if isinstance(rec_id, int) and (latest_rec_id is None or rec_id > latest_rec_id):
                latest_rec_id = rec_id

    filtered: List[dict] = []
    for event in parsed:
        event_kind = event.get("event")
        if kind == EVENT_KIND_NOTIFICATION and event_kind != EVENT_KIND_NOTIFICATION:
            continue
        if kind == EVENT_KIND_STARTUP and event_kind != EVENT_KIND_STARTUP:
            continue
        if since is not None:
            # Filtered reads are strictly rec_id-cursored: events without a
            # rec_id (startup lines) only ever appear on unfiltered reads —
            # clients would otherwise re-fetch them on every poll.
            rec_id = event.get("rec_id")
            if not isinstance(rec_id, int) or rec_id <= since:
                continue
        filtered.append(event)
        if len(filtered) >= limit:
            break
    return filtered, latest_rec_id


# ---------------------------------------------------------------------------
# Read-only SQLite finance reader (mode=ro, busy_timeout, no schema creation)
# ---------------------------------------------------------------------------


def readonly_ledger(db_path: str) -> FinanceLedger:
    """A FinanceLedger bound to a `mode=ro` URI connection with busy_timeout.

    Bypasses FinanceLedger.__init__ (which creates the schema — a side effect
    forbidden for the API). month_records/month_totals need neither the schema
    nor the rw connection. Callers must check the db exists first.
    """
    absolute = os.path.abspath(db_path)
    uri = "file:" + urllib.parse.quote(absolute) + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        conn.execute("PRAGMA busy_timeout=3000")
    except sqlite3.DatabaseError:  # pragma: no cover - defensive
        pass
    ledger = FinanceLedger.__new__(FinanceLedger)
    ledger.db_path = absolute
    ledger.conn = conn
    return ledger


def in_memory_ledger() -> FinanceLedger:
    """Empty schema-shaped ledger for report generation when finance.db is
    missing (zero file side effects)."""
    conn = sqlite3.connect(":memory:")
    conn.execute(LEDGER_SCHEMA)
    ledger = FinanceLedger.__new__(FinanceLedger)
    ledger.db_path = ":memory:"
    ledger.conn = conn
    return ledger


def sqlite_retry(fn: Callable):
    """Run a ledger read; single retry on 'database is locked' (daemon writes
    are short transactions; RESEARCH.md notes they never last long)."""

    def wrapped(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except sqlite3.OperationalError as e:
            if "database is locked" not in str(e):
                raise
            logger.warning("finance.db bloqueado; nova tentativa única em 150ms")
            time.sleep(0.15)
            return fn(*args, **kwargs)

    return wrapped


@sqlite_retry
def _ledger_totals(ledger: FinanceLedger, month: str) -> dict:
    return ledger.month_totals(month, kinds=None)


@sqlite_retry
def _ledger_records(ledger: FinanceLedger, month: str) -> List[dict]:
    return ledger.month_records(month, kinds=None)


def finance_report_path(config_dir: str, month: str) -> str:
    return os.path.normpath(os.path.join(config_dir, "reports", f"{month}.html"))


# ---------------------------------------------------------------------------
# API runtime: shared state for all request handlers
# ---------------------------------------------------------------------------


class BotApiRuntime:
    """Paths + per-request config loading. One instance per server."""

    api_version = "1.0"

    def __init__(self, config_path: str):
        self.config_path = os.path.abspath(config_path)
        self.config_dir = os.path.dirname(self.config_path) or "."

    # ---- path resolution (relative paths live beside config.json) ----

    def resolve(self, path_value: Optional[str], default: str) -> str:
        raw = str(path_value) if path_value is not None else default
        if raw.strip() == "":
            raw = default
        if not os.path.isabs(raw):
            raw = os.path.join(self.config_dir, raw)
        return os.path.normpath(raw)

    def log_file(self, config: dict) -> str:
        return self.resolve(config.get("log_file"), "logs/events.jsonl")

    def state_file(self, config: dict) -> str:
        return self.resolve(config.get("state_file"), ".bot_state.json")

    def ledger_db(self, config: dict) -> str:
        finance_cfg = config.get("finance") or {}
        return self.resolve(finance_cfg.get("ledger_db"), "finance.db")

    def reports_dir(self) -> str:
        return os.path.join(self.config_dir, "reports")

    # ---- shared reads ----

    def read_watermark(self, config: dict) -> Tuple[int, Optional[str]]:
        """(last_rec_id, updated_at) from the state file; zeroed when absent."""
        path = self.state_file(config)
        if not os.path.exists(path):
            return 0, None
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            last_rec_id = int(data.get("last_rec_id", 0))
            updated_at = data.get("updated_at")
            return last_rec_id, updated_at if isinstance(updated_at, str) else None
        except Exception as e:
            logger.warning("Falha ao ler state %s: %s", path, e)
            return 0, None

    def require_finance_enabled(self, config: dict) -> None:
        finance_cfg = config.get("finance") or {}
        if not finance_cfg.get("enabled", True):
            raise ApiError(
                409,
                "O plugin finance está desabilitado (config.finance.enabled = false).",
            )

    def build_reporter(self, config: dict, ledger: FinanceLedger) -> FinanceReporter:
        from notifybot.actions import resolve_telegram_config

        finance_cfg = config.get("finance") or {}
        tg_token, tg_chat_id = resolve_telegram_config(config.get("telegram"))
        return FinanceReporter(
            ledger=ledger,
            ollama_cfg=config.get("ollama", {}),
            categories=finance_cfg.get("categories") or [],
            telegram_summary=bool(finance_cfg.get("telegram_summary", False)),
            telegram_token=tg_token,
            telegram_chat_id=tg_chat_id,
            llm_timeout=float((config.get("router") or {}).get("timeout", 15.0)),
            reports_dir=self.reports_dir(),
        )

    # ---------- endpoint handlers ----------

    # #1 GET /api/health
    def health(self) -> dict:
        config = read_config_strict(self.config_path)
        last_rec_id, updated_at = self.read_watermark(config)
        log_path = self.log_file(config)
        log_bytes, log_mtime = 0, None
        try:
            stat = os.stat(log_path)
            log_bytes, log_mtime = stat.st_size, stat.st_mtime
        except OSError:
            pass
        finance_cfg = config.get("finance") or {}
        ollama_cfg = config.get("ollama") or {}
        return {
            "ok": True,
            "bot_version": __version__,
            "api_version": self.api_version,
            "watermark": {"last_rec_id": last_rec_id, "updated_at": updated_at},
            "log": {"path": log_path, "bytes": log_bytes, "mtime": log_mtime},
            "finance": {
                "enabled": bool(finance_cfg.get("enabled", True)),
                "db_exists": os.path.exists(self.ledger_db(config)),
            },
            "ollama": {
                "url": ollama_cfg.get("url", ""),
                "model": ollama_cfg.get("model", ""),
            },
        }

    # #2 GET /api/events
    def events(self, params: Dict[str, List[str]]) -> dict:
        config = read_config_strict(self.config_path)

        since: Optional[int] = None
        if "since" in params:
            raw_since = (params.get("since") or [""])[0].strip()
            try:
                since = int(raw_since)
            except (TypeError, ValueError):
                raise ApiError(
                    400, f"parâmetro 'since' inválido: {raw_since!r} (use um inteiro >= 0)"
                ) from None
            if since < 0:
                raise ApiError(
                    400, f"parâmetro 'since' inválido: {since} (use um inteiro >= 0)"
                ) from None

        limit_raw = (params.get("limit") or [str(DEFAULT_EVENT_LIMIT)])[0].strip()
        try:
            limit = int(limit_raw)
        except (TypeError, ValueError):
            raise ApiError(400, f"parâmetro 'limit' inválido: {limit_raw!r}") from None
        if not (MIN_EVENT_LIMIT <= limit <= MAX_EVENT_LIMIT):
            raise ApiError(
                400,
                f"parâmetro 'limit' deve estar entre {MIN_EVENT_LIMIT} e {MAX_EVENT_LIMIT}",
            ) from None

        kind = (params.get("kind") or [EVENT_KIND_ALL])[0].strip() or EVENT_KIND_ALL
        if kind not in VALID_EVENT_KINDS:
            raise ApiError(
                400, f"parâmetro 'kind' inválido: {kind!r} (use all | notification | startup)"
            ) from None

        log_path = self.log_file(config)
        events, latest_rec_id = read_events_tail(log_path, limit, since, kind)
        return {"events": events, "latest_rec_id": latest_rec_id, "count": len(events)}

    # #3 GET /api/config
    def config_get(self) -> dict:
        config = read_config_strict(self.config_path)
        return mask_config_secrets(config)

    # #4 POST /api/config
    def config_post(self, body: Any) -> dict:
        details = validate_config_payload(body)
        if details:
            raise ApiError(400, "config inválida", details)
        data: dict = dict(body)
        # Secrets never enter config.json via the API (env-only).
        data["telegram"] = {"token": "", "chat_id": ""}
        save_config_atomic(self.config_path, data)
        logger.info("Config salva via API: %s", self.config_path)
        return {"ok": True}

    # #5 GET /api/finance/summary
    def finance_summary(self, params: Dict[str, List[str]]) -> dict:
        config = read_config_strict(self.config_path)
        self.require_finance_enabled(config)
        month = self.month_from_params(params)
        db_path = self.ledger_db(config)
        if not os.path.exists(db_path):
            return {
                "month": month,
                "db_exists": False,
                "count": 0,
                "total": 0.0,
                "by_category": {},
                "by_kind": {},
                "prev_total": 0.0,
                "mom_delta": None,
            }
        ledger = readonly_ledger(db_path)
        try:
            totals = _ledger_totals(ledger, month)
            prev_totals = _ledger_totals(ledger, previous_month(month))
        finally:
            try:
                ledger.close()
            except Exception:  # pragma: no cover - close best-effort
                pass
        total = totals["total"]
        prev_total = prev_totals["total"]
        mom_delta: Optional[float] = None
        if prev_totals["count"] > 0 and prev_total > 0:
            mom_delta = _dec_to_float((total - prev_total) / prev_total * Decimal("100"))
        by_category = {cat: _dec_to_float(val) for cat, val in totals["by_category"].items()}
        by_kind = {name: _dec_to_float(val) for name, val in totals["by_kind"].items()}
        return {
            "month": month,
            "db_exists": True,
            "count": totals["count"],
            "total": _dec_to_float(total),
            "by_category": by_category,
            "by_kind": by_kind,
            "prev_total": _dec_to_float(prev_total),
            "mom_delta": mom_delta,
        }

    # #6 GET /api/finance/transactions
    def finance_transactions(self, params: Dict[str, List[str]]) -> dict:
        config = read_config_strict(self.config_path)
        self.require_finance_enabled(config)
        month = self.month_from_params(params)
        db_path = self.ledger_db(config)
        if not os.path.exists(db_path):
            return {"month": month, "records": []}
        ledger = readonly_ledger(db_path)
        try:
            raw_records = _ledger_records(ledger, month)
        finally:
            try:
                ledger.close()
            except Exception:  # pragma: no cover - close best-effort
                pass
        records = [
            {
                "rec_id": r["rec_id"],
                "ts": r["ts"],
                "bundle": r["bundle"],
                "amount": float(r["amount"]),
                "kind": r["kind"],
                "category": r["category"],
                "description": r["description"],
            }
            for r in raw_records
        ]
        return {"month": month, "records": records}

    # #7 GET /api/finance/report
    def finance_report_status(self, params: Dict[str, List[str]]) -> dict:
        config = read_config_strict(self.config_path)
        self.require_finance_enabled(config)
        month = self.month_from_params(params)
        html_path = finance_report_path(self.config_dir, month)
        return {"month": month, "html_path": html_path, "exists": os.path.exists(html_path)}

    # #8 POST /api/finance/report
    def finance_report_build(self, body: Any) -> dict:
        config = read_config_strict(self.config_path)
        self.require_finance_enabled(config)
        if body is None:
            body = {}
        if not isinstance(body, dict):
            raise ApiError(400, "corpo deve ser um objeto JSON opcional {month}")
        if "month" in body and body.get("month") is not None and not isinstance(body["month"], str):
            raise ApiError(400, "'month' deve ser uma string YYYY-MM")
        month = body.get("month")
        db_path = self.ledger_db(config)
        missing_db = not os.path.exists(db_path)
        try:
            ledger = in_memory_ledger() if missing_db else readonly_ledger(db_path)
            try:
                reporter = self.build_reporter(config, ledger)
                result = reporter.build(month)
            finally:
                try:
                    ledger.close()
                except Exception:  # pragma: no cover - close best-effort
                    pass
        except ApiError:
            raise
        except ValueError as e:  # validate_month invalid month
            raise ApiError(400, str(e)) from e
        except Exception as e:  # noqa: BLE001 - endpoint contract: 500 on build failure
            logger.error("Falha ao gerar relatório: %s", e)
            raise ApiError(500, f"falha ao gerar relatório: {e}") from e
        return {
            "ok": True,
            "month": result["month"],
            "html_path": result["html_path"],
            "markdown": result["markdown"],
        }

    # #9 POST /api/test-notify (body ignored; kept for uniform dispatch)
    def test_notify(self, body: Any = None) -> dict:
        from notifybot.cli import trigger_test_notifications

        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            trigger_test_notifications()
        posted = sum(
            1
            for line in stdout.getvalue().splitlines()
            if line.startswith("Triggered notification")
        )
        stderr_text = stderr.getvalue()
        if "Failed to post" in stderr_text:
            raise ApiError(500, stderr_text.strip() or "osascript falhou ao postar notificações")
        if posted == 0:
            raise ApiError(
                500, "nenhuma notificação de teste postada (osascript falhou silenciosamente)"
            )
        return {"ok": True, "posted": posted}

    # ---------- param helpers ----------

    # GET /api/logs/tail — raw tail support endpoint for the Logs screen
    # (plan addition: the ten-row table has no raw-tail route, but the Logs
    # acceptance criteria demand malformed lines rendered dimmed, which
    # requires raw text, not parsed dicts).
    def logs_tail(self, params: Dict[str, List[str]]) -> dict:
        config = read_config_strict(self.config_path)
        lines_raw = (params.get("lines") or [str(DEFAULT_LOG_TAIL_LINES)])[0].strip()
        try:
            lines = int(lines_raw)
        except (TypeError, ValueError):
            raise ApiError(400, f"parâmetro 'lines' inválido: {lines_raw!r}") from None
        if not (MIN_LOG_TAIL_LINES <= lines <= MAX_LOG_TAIL_LINES):
            raise ApiError(
                400,
                f"parâmetro 'lines' deve estar entre {MIN_LOG_TAIL_LINES} e {MAX_LOG_TAIL_LINES}",
            ) from None
        log_path = self.log_file(config)
        exists = os.path.exists(log_path)
        line_items = read_log_tail_lines(log_path, lines)
        return {
            "path": log_path,
            "exists": exists,
            "lines": line_items,
            "count": len(line_items),
        }

    def month_from_params(self, params: Dict[str, List[str]]) -> str:
        raw = (params.get("month") or [None])[0]
        try:
            return validate_month(raw)
        except ValueError as e:
            raise ApiError(400, str(e)) from e


# ---------------------------------------------------------------------------
# HTTP plumbing
# ---------------------------------------------------------------------------

# Each route maps to the BotApiRuntime handler method; params-style handlers
# receive the parsed query dict, POST handlers receive the parsed body.
ROUTES: Dict[str, Dict[str, str]] = {
    "GET": {
        "/api/health": "health",
        "/api/events": "events",
        "/api/config": "config_get",
        "/api/finance/summary": "finance_summary",
        "/api/finance/transactions": "finance_transactions",
        "/api/finance/report": "finance_report_status",
        "/api/logs/tail": "logs_tail",
    },
    "POST": {
        "/api/config": "config_post",
        "/api/finance/report": "finance_report_build",
        "/api/test-notify": "test_notify",
    },
}
QUERY_HANDLERS = frozenset({"events", "finance_summary", "finance_transactions", "finance_report_status", "logs_tail"})


class BotApiHTTPServer(ThreadingHTTPServer):
    """ThreadingHTTPServer with daemon threads: a long report build on one
    handler thread never blocks /api/health on another."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, host: str, port: int, runtime: BotApiRuntime):
        self.runtime = runtime
        super().__init__((host, port), BotApiHandler)


class BotApiHandler(BaseHTTPRequestHandler):
    server_version = f"NotifyBotAPI/{__version__}"

    # ---- helpers ----

    def cors_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", CORS_ALLOW_METHODS)
        self.send_header("Access-Control-Allow-Headers", CORS_ALLOW_HEADERS)
        self.send_header("Access-Control-Max-Age", CORS_MAX_AGE)

    def send_json(self, status: int, payload: dict) -> None:
        data = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.cors_headers()
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):  # pragma: no cover - client hangups
            pass

    def read_json_body(self) -> Any:
        length_raw = self.headers.get("Content-Length")
        if not length_raw:
            return None
        try:
            length = int(length_raw)
        except (TypeError, ValueError):
            raise ApiError(400, "Content-Length inválido") from None
        if length <= 0:
            return None
        if length > MAX_JSON_BODY_BYTES:
            raise ApiError(413, "corpo da requisição muito grande") from None
        raw = self.rfile.read(length).decode("utf-8", errors="replace")
        if raw.strip() == "":
            return None
        try:
            return json.loads(raw)
        except (ValueError, TypeError):
            raise ApiError(400, "corpo não é JSON válido") from None

    def log_message(self, fmt: str, *args: Any) -> None:  # request log at debug level
        logger.debug("%s - %s", self.address_string(), fmt % args)

    # ---- HTTP methods ----

    def do_OPTIONS(self) -> None:  # noqa: N802 (BaseHTTPRequestHandler API)
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.cors_headers()
        self.end_headers()

    def dispatch(self, method: str) -> None:
        runtime: BotApiRuntime = self.server.runtime  # type: ignore[attr-defined]
        parsed = urlparse(self.path)
        path = parsed.path or "/"
        params = parse_qs(parsed.query, keep_blank_values=True)

        if not any(path in methods for methods in ROUTES.values()):
            self.send_json(404, {"error": f"rota não encontrada: {path}"})
            return
        handler_name = ROUTES.get(method, {}).get(path)
        if handler_name is None:
            self.send_json(405, {"error": f"método {method} não permitido para {path}"})
            return

        try:
            if handler_name in QUERY_HANDLERS:
                payload = getattr(runtime, handler_name)(params)
            elif method == "POST":
                payload = getattr(runtime, handler_name)(self.read_json_body())
            else:
                payload = getattr(runtime, handler_name)()
        except ApiError as e:
            self.send_json(e.status, e.body)
            return
        except Exception as e:  # noqa: BLE001 - last-resort contract: JSON errors only
            logger.exception("Erro não tratado em %s %s", method, path)
            self.send_json(500, {"error": f"erro interno: {e}"})
            return
        self.send_json(200, payload)

    def do_GET(self) -> None:  # noqa: N802
        self.dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self.dispatch("POST")


# ---------------------------------------------------------------------------
# Entry point (called from notifybot.cli 'api' subcommand)
# ---------------------------------------------------------------------------


def env_port() -> int:
    raw = os.environ.get("NOTIFYBOT_API_PORT", "").strip()
    if raw == "":
        return DEFAULT_PORT
    try:
        return int(raw)
    except ValueError:
        logger.warning(
            "NOTIFYBOT_API_PORT inválido (%r); usando porta padrão %d", raw, DEFAULT_PORT
        )
        return DEFAULT_PORT


def serve(config_path: str = "config.json", host: Optional[str] = None, port: Optional[int] = None) -> int:
    """Run the API server; never spawns/manages the daemon. Returns an exit code."""
    resolved_host = str(host or DEFAULT_HOST)
    resolved_port = port if port is not None else env_port()

    if resolved_host not in LOOPBACK_HOSTS:
        print(
            f"[api] HOST REFUSADO: {resolved_host!r} — a API só aceita loopback "
            f"({'/'.join(sorted(LOOPBACK_HOSTS))}).",
            file=sys.stderr,
        )
        return 2
    if not isinstance(resolved_port, int) or not (1 <= resolved_port <= 65535):
        print(f"[api] PORTA inválida: {resolved_port!r} (use 1-65535)", file=sys.stderr)
        return 2

    runtime = BotApiRuntime(config_path)
    httpd = BotApiHTTPServer(resolved_host, resolved_port, runtime)
    print(f"Bot API ouvindo em http://{resolved_host}:{resolved_port} (Ctrl+C para sair)", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


def read_log_tail_lines(log_path: str, lines: int = DEFAULT_LOG_TAIL_LINES) -> List[Dict[str, Any]]:
    """Raw text tail of events.jsonl for the Logs screen.

    Returns [{line_no, raw, malformed: bool}] oldest-first, window-limited to
    `lines` entries; the file being absent yields [] (the UI shows the empty
    state). Malformed lines are flagged instead of dropped so the UI can dim
    them; line numbers are window-relative (they shift as the file grows).
    """
    lines = max(MIN_LOG_TAIL_LINES, min(MAX_LOG_TAIL_LINES, int(lines)))
    if not os.path.exists(log_path):
        return []
    try:
        size = os.path.getsize(log_path)
        start = max(0, size - MAX_EVENT_TAIL_BYTES)
        with open(log_path, "rb") as f:
            f.seek(start)
            data = f.read()
    except OSError as e:
        logger.warning("Falha ao ler %s: %s", log_path, e)
        return []

    text = data.decode("utf-8", errors="replace")
    if start > 0:
        if "\n" not in text:
            return []
        text = text.split("\n", 1)[1]  # skip the cut-off partial first line

    raw_lines = text.split("\n")
    # A trailing split element from the final newline is not a line; a file
    # without trailing newline keeps its last (possibly mid-write) line.
    if raw_lines and raw_lines[-1] == "" and text.endswith("\n"):
        raw_lines = raw_lines[:-1]
    window = raw_lines[max(0, len(raw_lines) - lines):]

    tail: List[Dict[str, Any]] = []
    for index, raw in enumerate(window):
        raw = raw.rstrip("\r")
        malformed = raw.strip() != "" and _parse_event_line(raw) is None
        tail.append(
            {
                "line_no": len(raw_lines) - len(window) + index + 1,
                "raw": raw,
                "malformed": malformed,
            }
        )
    return tail