"""In-process agent tool loop for the desktop Chat tab.

One turn = load/create session -> loop (<= MAX_TOOL_TURNS): global budget
check -> LLM call -> strict one-JSON-per-turn parse (one protocol-reminder
retry) -> tool dispatch or final answer -> append-only JSONL persistence
under ``<config_dir>/data/agent_sessions/<session_id>.jsonl``.

LLM injection: ``run_turn(runtime, session_id, message, llm_fn=None)``.
When the env var ``NOTIFYBOT_SMOKE_AGENT_MOCK=1`` is set, a deterministic
scripted LLM is used instead of Ollama (TEST/SMOKE-ONLY hook: unit tests
inject their own ``llm_fn``; the API smoke uses this env hook to exercise
the full dashboard pipeline without a live model).

Implemented in step 3 (skeleton in step 1).
"""

from notifybot.agent_tools import (
    ARTIFACT_HTML_MAX_BYTES,
    ARTIFACT_MAX_FILES,
    ARTIFACTS_PORT,
    MAX_TOOL_TURNS,
    SESSION_CONTEXT_MESSAGES,
    TOOL_RESULT_MAX_BYTES,
    TURN_BUDGET_S,
)

__all__ = [
    "ARTIFACT_HTML_MAX_BYTES",
    "ARTIFACT_MAX_FILES",
    "ARTIFACTS_PORT",
    "MAX_TOOL_TURNS",
    "SESSION_CONTEXT_MESSAGES",
    "TOOL_RESULT_MAX_BYTES",
    "TURN_BUDGET_S",
]