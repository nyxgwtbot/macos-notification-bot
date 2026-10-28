"""In-app agent tools layer (`app.data`, `dashboard.create`, `server.serve`,
`notify.test`, `native.call`) and the artifacts sub-service registry.

This module owns:

- the shared agent constants block (ports, budgets, caps) imported by
  ``notifybot.agent``;
- the four v1 tool implementations, backed by the EXISTING ``BotApiRuntime``
  handlers in ``notifybot/api.py`` (``health`` / ``events`` /
  ``finance_summary`` / ``finance_transactions`` / ``test_notify``) — zero
  duplicated query logic;
- the fifth tool ``native.call`` (connectors): envelope validation here, all
  execution delegated to ``notifybot.connectors.ConnectorManager`` (frozen
  argv-bound AppleScript, per-domain consent state, PT-BR consent errors);
- ``ArtifactServerRegistry`` — one stdlib ``ThreadingHTTPServer`` bound to
  loopback:4770 (``ARTIFACTS_PORT``; overridable via
  ``NOTIFYBOT_ARTIFACTS_PORT`` for tests, where the sandbox uses port 0) that
  serves ONLY ``<config_dir>/artifacts`` and answers a JSON health probe on
  ``GET /api/health``, so generated dashboard artifacts can be rendered in
  the desktop iframe.

Tool contract: every tool handler returns a JSON-serializable dict on
success; failures raise ``ToolFailure`` (or return/raise the runtime's
``ApiError``, mapped below) and surface to the model as ``{"error": "..."}``
results that count toward the turn cap — the loop itself never crashes on a
bad tool result.
"""

import functools
import json
import logging
import os
import threading
import uuid
from datetime import datetime, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse
from urllib.request import urlopen

logger = logging.getLogger("notification_bot")

# ---------------------------------------------------------------------------
# Shared agent constants (imported by notifybot.agent and notifybot.api)
# ---------------------------------------------------------------------------

ARTIFACTS_PORT = 4770
MAX_TOOL_TURNS = 6
TURN_BUDGET_S = 90.0
TOOL_RESULT_MAX_BYTES = 4096
SESSION_CONTEXT_MESSAGES = 8
ARTIFACT_HTML_MAX_BYTES = 512_000
ARTIFACT_MAX_FILES = 200

# Health self-ping budget for the artifacts server (fast — loopback only).
ARTIFACTS_PING_TIMEOUT_S = 1.5
# Marker returned by the artifacts health probe: guarantees a healthy ping is
# OUR server (guards against adopting a foreign process that happens to own
# the port with its own /api/health).
ARTIFACT_SERVICE_MARKER = "notifybot-artifacts"

DASHBOARD_TITLE_MAX_CHARS = 120
# Environment override for the artifacts port. Production keeps the fixed
# constant 4770; the test suite sets 0 so every fixture gets an ephemeral
# port and the url reports the actually-bound port.
ARTIFACTS_PORT_ENV = "NOTIFYBOT_ARTIFACTS_PORT"

# Patterns rejected in dashboard HTML: scripts, external references, and
# network-fetching constructs. Matched case-insensitively over the payload.
FORBIDDEN_HTML_PATTERNS: Tuple[str, ...] = (
    "<script",
    "<link",
    "@import",
    "url(http",
    "fetch(",
    "xmlhttprequest",
    'src="http://', 'src="https://', "src='http://", "src='https://",
    'href="http://', 'href="https://', "href='http://", "href='https://",
)


class ToolFailure(Exception):
    """Raised by tool implementations; the message becomes the tool error result."""


class ArtifactServerRegistry:
    """One in-process loopback artifacts server + idempotent lifecycle ops.

    ``ensure`` is the only op the agent loop calls: healthy self-ping ->
    reuse; else start (or restart a stalled listener). Host and port are
    fixed constructor constants — the model never gets reach to them.
    """

    def __init__(self, artifacts_dir: str, port: int = ARTIFACTS_PORT, host: str = "127.0.0.1"):
        self.root = artifacts_dir
        self.requested_port = int(port)
        self.host = host
        self._server: Optional["ArtifactHTTPServer"] = None
        self._thread: Optional[threading.Thread] = None
        self._started_since: Optional[str] = None
        self._actual_port: Optional[int] = None
        self._lock = threading.Lock()

    # ---- lifecycle ----

    def ensure(self, artifact_id: Optional[str] = None) -> dict:
        """Idempotently have a healthy artifacts server; return its url info.

        With ``artifact_id``, verifies ``<artifact_id>.html`` exists under the
        artifacts root before returning ``{"url": "http://<host>:<port>/<id>.html"}``.
        Raises ``ToolFailure`` when the port cannot be bound or the artifact
        does not exist.
        """
        with self._lock:
            if self._server is None:
                self._start_locked()
            elif not self._health_ping():
                logger.warning("Artifacts server stall detectado; reiniciando listener na porta %s", self._actual_port)
                self._shutdown_locked()
                self._start_locked()
            port = self._actual_port
        if artifact_id is not None:
            artifact_path = os.path.join(self.root, f"{artifact_id}.html")
            if not os.path.isfile(artifact_path):
                raise ToolFailure(
                    f"artifact not found: {artifact_id}.html (create it with dashboard.create first)"
                )
            return {"url": f"http://{self.host}:{port}/{artifact_id}.html"}
        return {"url": f"http://{self.host}:{port}/", "running": True}

    def stop(self) -> None:
        with self._lock:
            self._shutdown_locked()

    def list(self) -> dict:
        """Registry status row for GET /api/services."""
        with self._lock:
            return {
                "running": self._server is not None,
                "host": self.host,
                "port": self._actual_port if self._server is not None else self.requested_port,
                "root": self.root,
                "since": self._started_since,
            }

    # ---- internals (all called under self._lock) ----

    def _start_locked(self) -> None:
        try:
            server = ArtifactHTTPServer(
                (self.host, self.requested_port),
                functools.partial(ArtifactHTTPRequestHandler, directory=self.root),
            )
        except OSError as e:
            raise ToolFailure(
                f"artifacts port {self.requested_port} busy — close the other process (bind failed: {e})"
            ) from e
        thread = threading.Thread(
            target=server.serve_forever, name="notifybot-artifacts", daemon=True
        )
        thread.start()
        self._server = server
        self._thread = thread
        self._actual_port = int(server.server_address[1])
        self._started_since = datetime.now(timezone.utc).isoformat()
        logger.info("Artifacts server em http://%s:%s (root=%s)", self.host, self._actual_port, self.root)

    def _shutdown_locked(self) -> None:
        if self._server is None:
            return
        self._server.shutdown()
        self._server.server_close()
        self._server = None
        self._thread = None
        self._actual_port = None
        self._started_since = None

    def _health_ping(self) -> bool:
        """Self-ping GET /api/health; True only when OUR marker answers."""
        if self._server is None or self._actual_port is None:
            return False
        try:
            with urlopen(
                f"http://{self.host}:{self._actual_port}/api/health",
                timeout=ARTIFACTS_PING_TIMEOUT_S,
            ) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            return data.get("ok") is True and data.get("service") == ARTIFACT_SERVICE_MARKER
        except Exception:  # noqa: BLE001 - any probe failure means unhealthy
            return False


class ArtifactHTTPServer(ThreadingHTTPServer):
    """ThreadingHTTPServer for artifacts: a slow client never blocks others."""

    daemon_threads = True
    allow_reuse_address = True


class ArtifactHTTPRequestHandler(SimpleHTTPRequestHandler):
    """Static file server for <config_dir>/artifacts + a JSON health probe.

    Only generated artifacts live in the served directory, so
    SimpleHTTPRequestHandler's auto content-type mapping is all we need.
    """

    def do_GET(self) -> None:  # noqa: N802 (BaseHTTPRequestHandler API)
        if urlparse(self.path).path == "/api/health":
            body = json.dumps({"ok": True, "service": ARTIFACT_SERVICE_MARKER}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):  # pragma: no cover
                pass
            return
        super().do_GET()

    def log_message(self, fmt: str, *args: Any) -> None:  # request log at debug level
        logger.debug("artifacts %s - %s", self.address_string(), fmt % args)


def artifacts_dir_for(config_dir: str) -> str:
    """Artifacts root resolved under the config dir (sandbox-friendly)."""
    return os.path.join(config_dir, "artifacts")


def artifacts_port_from_env(default: int = ARTIFACTS_PORT) -> int:
    """NOTIFYBOT_ARTIFACTS_PORT override (tests inject 0; default stays 4770)."""
    raw = os.environ.get(ARTIFACTS_PORT_ENV, "").strip()
    if raw == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("%s inválida (%r); usando porta padrão %d", ARTIFACTS_PORT_ENV, raw, default)
        return default
    if not (0 <= value <= 65535):
        logger.warning("%s fora do intervalo 0-65535 (%d); usando %d", ARTIFACTS_PORT_ENV, value, default)
        return default
    return value


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------


def _coerce_params(params: Any) -> Dict[str, List[str]]:
    """Flatten model-supplied args into the Dict[str, List[str]] shape the
    existing QUERY_HANDLERS already consume (parse_qs semantics)."""
    if not isinstance(params, dict):
        return {}
    result: Dict[str, List[str]] = {}
    for key, value in params.items():
        if value is None:
            continue  # absent is absent
        if isinstance(value, list):
            result[str(key)] = [str(item) for item in value]
        else:
            result[str(key)] = [str(value)]
    return result


def _api_error_result(api_error: Any) -> dict:
    """Map the runtime's ApiError (duck-typed: status + error + details) to a
    tool error result; existing PT-BR endpoint strings pass through verbatim."""
    result: Dict[str, Any] = {"error": api_error.error, "status": api_error.status}
    details = getattr(api_error, "details", None)
    if details:
        result["details"] = list(details)
    return result


class ToolRegistry:
    """Dispatch table over the four v1 tools + native.call (connectors).
    One per server process."""

    def __init__(
        self,
        runtime: Any,
        server_registry: ArtifactServerRegistry,
        connector_manager: Optional[Any] = None,
    ):
        self.runtime = runtime
        self.server_registry = server_registry
        # Connectors executor (native.call). Optional third param so both
        # existing construction sites keep compiling unchanged; when omitted
        # it is lazily built from runtime.config_dir on the first native.call.
        self.connector_manager = connector_manager

    def _ensure_connector_manager(self) -> Any:
        """Lazy singleton for the ConnectorManager (avoids importing/connecting
        anything at registry construction time)."""
        if self.connector_manager is None:
            from notifybot.connectors import ConnectorManager  # lazy (no cycle)

            self.connector_manager = ConnectorManager(self.runtime.config_dir)
        return self.connector_manager

    def dispatch(self, name: str, args: Any, ctx: Optional[Dict[str, Any]] = None) -> dict:
        """Run one tool; unknown tools and failures return {"error": ...}
        results (they count toward the MAX_TOOL_TURNS cap so the model can
        self-correct). ``ctx`` is reserved per-turn metadata (v1: unused)."""
        del ctx  # v1 carries no per-tool budget data beyond the loop's checks
        if name == "app.data":
            return self._protected(self._app_data, name, args)
        if name == "dashboard.create":
            return self._protected(self._dashboard_create, name, args)
        if name == "server.serve":
            return self._protected(self._serve, name, args)
        if name == "notify.test":
            return self._protected(self._notify_test, name, args)
        if name == "native.call":
            return self._protected(self._native_call, name, args)
        return {
            "error": f"unknown tool: {name!r} (valid: app.data, dashboard.create, server.serve, notify.test, native.call)"
        }

    def _protected(self, handler, tool_name: str, args: Any) -> dict:
        """Uniform failure boundary: ApiError -> error result; ToolFailure ->
        error result; anything else is logged and degraded to an error result
        (the loop itself never crashes on a tool)."""
        from notifybot.api import ApiError  # lazy (api owns the loop wiring)

        try:
            return handler(args if isinstance(args, dict) else {})
        except ApiError as e:
            return _api_error_result(e)
        except ToolFailure as e:
            return {"error": str(e)}
        except Exception as e:  # noqa: BLE001 - tool contract: errors, never crashes
            logger.exception("Tool %s falhou inesperadamente", tool_name)
            return {"error": f"tool {tool_name!r} failed internally: {e}"}

    # ---- app.data ----

    APP_DATA_HANDLERS: Dict[str, Tuple[str, bool]] = {
        "health": ("health", False),
        "events": ("events", True),
        "finance_summary": ("finance_summary", True),
        "finance_transactions": ("finance_transactions", True),
    }

    def _app_data(self, args: dict) -> dict:
        """Read datasets through the EXISTING runtime handlers — the same
        code paths as the JSON API endpoints, no duplicated query logic."""
        dataset = args.get("dataset")
        if not isinstance(dataset, str) or dataset.strip() == "":
            raise ToolFailure(
                "app.data requires a 'dataset' string "
                "(health, events, finance_summary, finance_transactions)"
            )
        entry = self.APP_DATA_HANDLERS.get(dataset.strip())
        if entry is None:
            raise ToolFailure(
                f"unknown dataset: {dataset!r} "
                "(valid: health, events, finance_summary, finance_transactions)"
            )
        handler_name, takes_params = entry
        handler = getattr(self.runtime, handler_name)
        if takes_params:
            return handler(_coerce_params(args.get("params")))
        return handler()

    # ---- dashboard.create ----

    def _dashboard_create(self, args: dict) -> dict:
        """Validate + persist a self-contained HTML artifact; server-side id.

        The model supplies title/html/data_source only — ids and file paths
        are generated here (uuid4 hex) so the model can never address the
        filesystem outside the artifacts root.
        """
        title = args.get("title")
        html = args.get("html")
        data_source = args.get("data_source")

        if not isinstance(title, str) or title.strip() == "":
            raise ToolFailure("dashboard.create requires a non-empty 'title' string")
        title = title.strip()
        if len(title) > DASHBOARD_TITLE_MAX_CHARS:
            raise ToolFailure(f"'title' is limited to {DASHBOARD_TITLE_MAX_CHARS} characters")
        if not isinstance(html, str) or html.strip() == "":
            raise ToolFailure("dashboard.create requires a non-empty 'html' string (complete HTML document)")
        if not isinstance(data_source, str) or data_source.strip() == "":
            raise ToolFailure("dashboard.create requires a 'data_source' string naming the datasets used")
        html_bytes = len(html.encode("utf-8"))
        if html_bytes > ARTIFACT_HTML_MAX_BYTES:
            raise ToolFailure(
                f"'html' is too large: {html_bytes} bytes (limit {ARTIFACT_HTML_MAX_BYTES} bytes)"
            )
        lowered = html.lower()
        for pattern in FORBIDDEN_HTML_PATTERNS:
            if pattern in lowered:
                raise ToolFailure(
                    f"'html' rejected: pattern {pattern!r} is not allowed "
                    "(self-contained HTML with inline CSS/SVG only — no scripts, "
                    "no external references, no network calls)"
                )

        artifact_id = uuid.uuid4().hex
        directory = artifacts_dir_for(self.runtime.config_dir)
        os.makedirs(directory, exist_ok=True)
        artifact_path = os.path.join(directory, f"{artifact_id}.html")
        _write_atomic(artifact_path, html)
        pruned = prune_artifacts(directory, ARTIFACT_MAX_FILES)
        if pruned:
            logger.info("Pruned %d old artifacts (cap %d)", pruned, ARTIFACT_MAX_FILES)
        return {"artifact_id": artifact_id, "path": artifact_path}

    # ---- server.serve ----

    def _serve(self, args: dict) -> dict:
        """Publish the artifact: ensure the sub-service is healthy and verify
        the file exists before returning the loopback URL."""
        artifact_id = args.get("artifact_id")
        if not isinstance(artifact_id, str) or artifact_id.strip() == "":
            raise ToolFailure(
                "server.serve requires an 'artifact_id' string "
                "(the id returned by dashboard.create)"
            )
        return self.server_registry.ensure(artifact_id.strip())

    # ---- notify.test ----

    def _notify_test(self, args: dict) -> dict:
        """Delegate to the existing runtime.test_notify (which wraps
        cli.trigger_test_notifications).

        Side effects documented for the model's benefit: this posts THREE
        REAL macOS notifications via osascript, and a live daemon poll cycle
        may consume them (advancing the watermark). osascript failures
        surface as the runtime's ApiError -> {"error": ...} tool result.
        """
        return self.runtime.test_notify(args if isinstance(args, dict) else None)


    # ---- native.call (connectors) ----

    def _native_call(self, args: dict) -> dict:
        """Drive a native macOS app through the approved connectors whitelist.

        Validates the envelope (domain/action strings + params dict), then
        delegates to the ConnectorManager (whitelist -> consent gate ->
        argv-bound osascript -> outcome classification). ConnectorError
        messages (PT-BR consent/denial/timeout, English validation) surface
        VERBATIM as the tool error result — the agent relays them, never
        retries an ungranted domain.
        """
        domain = args.get("domain")
        if not isinstance(domain, str) or domain.strip() == "":
            raise ToolFailure("native.call requires a 'domain' string (reminders, notes, system)")
        action = args.get("action")
        if not isinstance(action, str) or action.strip() == "":
            raise ToolFailure("native.call requires an 'action' string for the domain")
        params = args.get("params")
        if params is None:
            params = {}
        if not isinstance(params, dict):
            raise ToolFailure("native.call 'params' must be a JSON object")
        from notifybot.connectors import ConnectorError  # lazy (no import cycle)

        try:
            return self._ensure_connector_manager().run_action(domain.strip(), action.strip(), params)
        except ConnectorError as e:
            raise ToolFailure(str(e)) from None


def _write_atomic(path: str, content: str) -> None:
    """Atomic file write: tmp file (same dir) + fsync + os.replace, mirroring
    notifybot/api.py save_config_atomic. Never leaves a torn file."""
    tmp_path = f"{path}.tmp.{os.getpid()}.{uuid.uuid4().hex[:8]}"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except Exception:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except OSError:  # pragma: no cover - defensive
            pass
        raise


def prune_artifacts(directory: str, cap: int = ARTIFACT_MAX_FILES) -> int:
    """Rotation guard: keep the newest `cap` artifact files (mtime sort) and
    delete the rest. Returns the number of removed files."""
    if not os.path.isdir(directory):
        return 0
    entries: List[Tuple[float, str]] = []
    for name in os.listdir(directory):
        if not name.endswith(".html"):
            continue
        path = os.path.join(directory, name)
        if not os.path.isfile(path):
            continue
        try:
            entries.append((os.stat(path).st_mtime, path))
        except OSError:  # pragma: no cover - raced deletion
            continue
    if len(entries) <= cap:
        return 0
    entries.sort(reverse=True)  # newest first
    removed = 0
    for _, path in entries[cap:]:
        try:
            os.remove(path)
            removed += 1
        except OSError:  # pragma: no cover - best-effort rotation
            continue
    return removed