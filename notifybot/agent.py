"""In-process agent tool loop for the desktop Chat tab.

One turn = load/create session -> loop (<= MAX_TOOL_TURNS): global budget
check -> LLM call -> strict one-JSON-per-turn parse (one protocol-reminder
retry) -> tool dispatch or final answer -> append-only JSONL persistence
under ``<config_dir>/data/agent_sessions/<session_id>.jsonl``.

LLM injection: ``run_turn(runtime, session_id, message, llm_fn=None)``.
``llm_fn(messages) -> str`` — an exception raised by ``llm_fn`` is converted
into a graceful PT-BR error answer (the caller keeps HTTP 200; the loop
itself never turns an LLM failure into a 500).

Mock hook: when the env var ``NOTIFYBOT_SMOKE_AGENT_MOCK=1`` is set,
``default_llm_fn`` selects a deterministic scripted mock LLM that walks the
full dashboard pipeline (app.data -> dashboard.create -> server.serve ->
answer with urls) for any "dashboard" request and uses ``notify.test`` for
"test notification" requests. This is a TEST/SMOKE-ONLY hook: unit tests
inject their own ``llm_fn`` transcript stubs; the API smoke
(``scripts/smoke_api.py``) uses the env hook to exercise the server-side
pipeline without a live Ollama model.
"""

import json
import logging
import os
import re
import time
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

PROMPT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "agent_prompt.md")

PROTOCOL_REMINDER = (
    "Your previous reply was NOT one valid protocol JSON object. Reply again with "
    'EXACTLY ONE JSON object \u2014 either {"tool": "<name>", "args": {...}} or '
    '{"answer": "<pt-br text>", "urls": [...]} \u2014 with no prose and no markdown fences.'
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
    "Valid tools: app.data, dashboard.create, server.serve, notify.test. "
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
            rendered.append(
                {
                    "role": "assistant",
                    "content": json.dumps(
                        {"answer": record.get("answer", ""), "urls": record.get("urls", [])},
                        ensure_ascii=False,
                    ),
                }
            )
        elif role == "assistant" and "tool" in record:
            lines = [
                "tool_call: "
                + json.dumps({"tool": record.get("tool"), "args": record.get("args", {})}, ensure_ascii=False)
            ]
            if record.get("error") is not None:
                lines.append("tool_error: " + json.dumps({"error": record.get("error")}, ensure_ascii=False))
            else:
                result = record.get("result")
                lines.append(
                    "tool_result: "
                    + truncate_tool_result_text(json.dumps(result, ensure_ascii=False, default=str))
                )
            rendered.append({"role": "assistant", "content": "\n".join(lines)})
        # unknown record shapes (future) are skipped, never crash the loop
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


def _ollama_chat(runtime: Any, messages: List[dict]) -> str:
    """One POST to config.ollama.url ({model, stream:false, messages}) —
    same wire format as notifybot.classifier.LLMClassifier. Raises on any
    failure; run_turn converts that into a graceful error answer."""
    from notifybot.api import read_config_strict  # lazy (api wires the loop)

    config = read_config_strict(runtime.config_path)
    ollama_cfg = config.get("ollama") or {}
    url = ollama_cfg.get("url") or "http://127.0.0.1:11434/api/chat"
    model = ollama_cfg.get("model") or "glm-5.3-flash:cloud"
    timeout = float(ollama_cfg.get("timeout", 30.0))
    payload = {"model": model, "stream": False, "messages": messages}
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return str((data.get("message") or {}).get("content", ""))


MOCK_DASHBOARD_TITLE = "Agent Health Overview"
MOCK_DASHBOARD_DATA_SOURCE = "app.data:health"


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
        if message.get("role") == "user":
            user_text = str(message.get("content", ""))
            break

    tool_calls = 0
    last_call_name: Optional[str] = None
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


def run_turn(
    runtime: Any,
    session_id_or_none: Optional[str],
    message: str,
    llm_fn: Optional[Callable[[List[dict]], str]] = None,
) -> dict:
    """Run one synchronous agent turn; returns
    ``{session_id, turn: [{role, tool, args, result?|error?, ts}...],
    answer, urls[]}``.

    Failure paths (LLM error, unparsable-with-retry, max turns, budget
    exceeded) all produce PT-BR error answers with the session preserved on
    disk — the loop never raises for content/model failures.
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

    user_record = {"role": "user", "content": message}
    store.append(session_id, user_record)
    records.append(user_record)

    resolve_llm = llm_fn if llm_fn is not None else default_llm_fn(runtime)
    system_prompt = load_system_prompt()
    registry = _tool_registry_for(runtime)

    started = time.monotonic()
    turn_steps: List[dict] = []
    answer_text: Optional[str] = None
    urls: List[str] = []

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

        try:
            raw = resolve_llm(build_context(system_prompt, records))
        except Exception as e:  # noqa: BLE001 - graceful LLM failure, never a 500
            logger.warning("Agent LLM call failed: %s", e)
            answer_text = llm_down_answer
            break

        parsed = parse_model_json(raw)
        if parsed is None:
            # One protocol-reminder retry (does not consume a tool turn; the
            # reminder is NOT persisted to the session).
            try:
                retry_raw = resolve_llm(
                    build_context(system_prompt, records) + [{"role": "user", "content": PROTOCOL_REMINDER}]
                )
            except Exception as e:  # noqa: BLE001
                logger.warning("Agent LLM protocol-retry failed: %s", e)
                answer_text = llm_down_answer
                break
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
            records.append(step)
            turn_steps.append(step)
            continue

        answer_text = parsed["answer"]
        urls = list(parsed.get("urls", []))
        break

    if answer_text is None:
        answer_text = (
            f"Desculpe, atingi o limite de {MAX_TOOL_TURNS} passos de ferramenta nesta resposta "
            "sem concluir a tarefa. Peça de novo e eu continuo a partir daqui."
        )

    answer_record = {"role": "assistant", "answer": answer_text, "urls": urls}
    store.append(session_id, answer_record)

    return {
        "session_id": session_id,
        "turn": turn_steps,
        "answer": answer_text,
        "urls": urls,
    }