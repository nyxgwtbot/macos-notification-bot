# Evidence — MVP end-to-end proof (2026-10-24/25)

## Gates run by the supervisor (not self-reported)
- Unit tests: `python3 -m unittest discover -s tests` → Ran 21 tests, OK.
- `python3 bot.py --dump` → parsed recent records (title/subtitle/body/app/uuid/date).
- `python3 bot.py --once` → exit 0, startup event in logs/events.jsonl.
- `python3 scripts/smoke_e2e.py` → SMOKE RESULT: PASSED (received → classified → routed, telegram SKIPPED acceptable w/o credentials).

## Live supervised smoke (with credentials, real Telegram delivery)
Notification posted on the OS at runtime:
`display notification "Transferencia de R$ 500 confirmada em sua conta corrente" with title "BOT-LIVE-TEST" subtitle "banco"`
Result in `logs/events.jsonl` (rec_id 8):
{"title":"BOT-LIVE-TEST","classification":{"category":"alert","urgency":"high","summary":"Bank transfer of R$ 500 confirmed in your checking account."},"rule":"high_urgency","actions":["log","telegram"],"action_results":{"telegram":"ok","log":"ok"}}
→ Telegram message delivered to the operator chat (action_results.telegram == "ok").

## E2E smoke (from scripts/smoke_e2e.py)
Two notifications (SMOKE-E2E-1 promo-style, SMOKE-E2E-2 urgent-alert-style) classified live by
glm-5.3-flash:cloud (messages: "Your package has shipped and promo code E2E20 is now active." / "Smoke
test notification simulating an urgent security alert requesting account verification.") and routed to
the correct rules. Full JSONL lines: smoke-events.jsonl.

## Pipeline latency measured
OS post → db2 write: ~2–6 s. Poll interval: 2 s (default config).

# Evidence — Agent & Chat tool pipeline (2026-10-27, feat/agent-tools)

## Gates run during implementation
- `python3 -m unittest discover -s tests` → Ran 138 tests, OK (112 pre-existing + 26 new agent tests).
- `python3 scripts/smoke_api.py` (core rows, mock env off) → SMOKE RESULT: PASSED.
- `NOTIFYBOT_SMOKE_AGENT_MOCK=1 python3 scripts/smoke_api.py` → SMOKE RESULT: PASSED (run twice, idempotent).
- Renderer: `cd desktop && npm run test:unit -- --run` → 70 passed (54 pre-existing + 16 chat view-model);
  `npm run lint` clean; `npx vue-tsc --noEmit` clean; `npm run desktop:build` end-to-end clean.

## Mock-mode agent pipeline transcript (server-side, sandboxed)
POST /api/agent/turn {"message": "dashboard, por favor"} (scripted LLM walks
app.data → dashboard.create → server.serve → answer):
- turn tools order: ["app.data", "dashboard.create", "server.serve"]; roles all "assistant".
- answer: "Aqui está o dashboard 'Agent Health Overview' com os dados do bot..."
- urls[0]: http://127.0.0.1:4770/1bfb7889745b4a54b670a351680964f4.html
- raw GET urls[0] → 200, text/html, 445 bytes, body contains "<title>Agent Health Overview</title>".
- GET /api/services → artifacts row: {"running": true, "host": "127.0.0.1", "port": 4770, root=<sandbox>/artifacts, since=...}.
- GET /api/agent/sessions → exactly 1 session, preview "dashboard, por favor".
- GET /api/agent/session/<id> → roles ["user", "assistant", "assistant", "assistant", "assistant"].
Second run produced a fresh artifact id (dee7216116ce46e68c24d973b5c9ba03.html) — no state reuse
(tempdir sandbox per run; rotation guard keeps newest 200 artifacts per config dir).

## Not self-reported (supervisor real-window checklist pending)
True-LLM turn, osascript consent identity under Electron, iframe pixel painting, and
daemon consumption of the 3 test notifications are supervisor checklist items
(trailing block in scripts/smoke_api.py), not part of this automated run.
