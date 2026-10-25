# Reflection: LLM-call budget in e2e smoke runs (macos-notification-bot)

The notification-bot MVP allows **at most one live Ollama classification per
session**. This budget is protected by two repo designs that must not drift:

- `scripts/smoke_e2e.py` is **idempotent**: per-marker reuse means a rerun never
  re-posts notifications whose full evidence (received/classified/routed) is
  already in `logs/events.jsonl` — reruns consume zero LLM calls. Never weaken
  this into "always post both markers"; that reintroduces an LLM call per rerun.
- `tests/test_bot.py` is **fully offline** — every `urllib.request.urlopen` call
  is mocked and no test invokes osascript/Notification Center. Keep it that way;
  a "convenient" real-HTTP test breaks the whole verification loop.

Practical corollaries learned the hard way in the 2026-10-24 session:
- Run the smoke script only once per session at first; every later run relies on
  the reuse path.
- Verification order matters: schedule any standalone `bot.py --once` AFTER the
  smoke pass, so stray pending notifications can't trigger a second live
  classification.
- osascript-posted notifications can take ~2–6 s to appear as db2 rows
  (RESEARCH.md §A); e2e scripts must settle ≥6 s and/or use a bounded bot-retry
  (≤2 runs) instead of failing fast.
- The event log has only two event types (`startup`, `notification`); pipeline
  stage evidence (classified/routed/telegram) lives INSIDE the notification
  event, not as separate stage events.