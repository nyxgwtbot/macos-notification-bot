# Agent loop v1 — learnings

- **Single source of truth beats module cleanliness**: the tool layer needed `ApiError`
  from `notifybot/api.py` while the API imports the tool layer — solved with lazy
  `from notifybot.api import ApiError` inside the dispatch boundary. No circular import
  at module level; the API module is fully loaded long before any request runs.
- **Port-0 injection is the whole test story**: `ArtifactServerRegistry` takes the port
  as a constructor constant; `NOTIFYBOT_ARTIFACTS_PORT=0` (read lazily at registry
  creation inside `ensure_agent_registry`) lets the test suite get an ephemeral bound
  port carried back in `url` — tests never bind 4770 and never race each other for it.
- **Record-then-stamp ts discipline**: session records get their `ts` inside
  `AgentSessionStore.append` (mutating the caller's dict) so the JSONL line, the
  in-memory context, and the HTTP response all share one timestamp — avoid stamping
  separately or they drift.
- **Transcript-in-one-message context**: each tool step renders as a single assistant
  message (`tool_call: {...}` + `tool_result/tool_error: {...}` lines) instead of
  separate assistant/tool roles — safest for small chat models without native tool
  calling, and it keeps "1 record = 1 context message" so the last-8 window is exactly
  countable. The mock LLM and tests parse these same line prefixes.
- **Test-only mock hook**: `NOTIFYBOT_SMOKE_AGENT_MOCK=1` (forwarded into the smoke
  server subprocess env) selects a deterministic scripted LLM in `agent.py`; unit tests
  inject `llm_fn` directly instead. Never let the mock silently apply when an explicit
  `llm_fn` was injected — injection wins.
- **unittest method order is alphabetical**: class-level API fixtures share state across
  test methods, so a test asserting "not started yet" can run after another test that
  started the sub-service — write API-level shape assertions order-robustly (or use
  fresh fixtures) instead of assuming clean state.
- **Vue iframe TS gotcha**: `sandbox` must be `sandbox=""` (string), not a bare
  attribute — vue-tsc types it as string, and the empty value is exactly the
  no-scripts sandbox semantics we want.
- **Sandboxed API-server fixtures + lazy sub-services**: agent handlers call
  `ensure_agent_registry()` which binds nothing; the artifacts listener starts only on
  the first `server.serve`. That keeps `GET /api/services` side-effect-free in tests.