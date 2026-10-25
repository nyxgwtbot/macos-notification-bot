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
