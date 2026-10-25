# HANDOFF — Finish notification-bot MVP (state from prior agent)

## HANDOFF STATE (do not revert, reconcile)
- 5 commits already on main (config, reader, state/dedupe, classifier, routing — PLAN.md steps 1-5 done, `bot.py` 884 lines, `config.json` present, `.gitignore` ignores `.bot_state.json` and `logs/`).
- UNTRACKED WIP: `tests/` (`tests/test_bot.py`, 411 lines, 20 tests) — DO NOT delete; fix and commit it.
- Missing deliverables: `scripts/smoke_e2e.py` (PLAN step 7), `README.md` (PLAN step 8), green tests (PLAN step 6).

## Your tasks (in order; commit after EACH)
1. Fix the 2 test failures in `tests/test_bot.py`:
   - ERROR at line 238 (`test_classification_fallback_on_network_or_parse_error`): `NameError: name 'urllib' is not defined` — the test file must import urllib.error explicitly (e.g. `import urllib.error`) — that test mocks urlopen of bot.py.
   - FAIL at line 104 (`test_snapshot_and_read_synthetic_db`): `self.assertIn(1, reader.app_cache)` failed ({} empty). Reconcile the CONTRACT: after reading a snapshot, the reader must map app_id->identifier taken from the `app` table of the snapshot into `app_cache`. Change MINIMAL code in `bot.py` (populating the cache from the snapshot's app table) OR the test if the reader's design deliberately avoids it — but the cache behavior must be consistent with how `bot.py` resolves bundle ids: prefer making the reader fill `app_cache` from the snapshot's app table.
2. Run `python3 -m unittest discover -s tests 2>&1`; iterate until ALL 20 pass (network still not called — telegram mocked). Commit tests (message: `test: unit suite green (20 tests) — parser, reader, state, classifier fallback, router, actions`).
3. Implement `scripts/smoke_e2e.py` per PLAN step 7: posts 2 demo notifications via osascript, runs `python3 bot.py --once`, waits, then verifies `logs/events.jsonl` shows received → classified → routed events with non-empty AI summary; prints PASSED/FAILED lines with evidence; telegram SKIPPED is OK (not failure) when env is missing. Make it runnable on system python3 3.9. Commit (`feat: e2e smoke script`).
4. Write `README.md` per PLAN step 8: what it does, one-command run (e.g. `python3 bot.py`), config keys documented, rules examples, `--dump` troubleshooting, permissions (zero TCC), iPhone-notes section (Monterey: SMS/iMessage relay via Messages only; full mirroring needs macOS Sequoia+, entries land in the same db2 automatically), event log format. Commit (`docs: README with setup, rules, troubleshooting`).
5. Final gates, print a summary block:
   - `python3 -m unittest discover -s tests` (all green)
   - `python3 bot.py --dump` (prints recent records, no network)
   - `python3 bot.py --once` exits 0 with a startup event in logs/events.jsonl
   - `python3 scripts/smoke_e2e.py` (may itself run bot --once)
   - `git status` clean; `git log --oneline` shows your commits.
   Do NOT run live classification against ollama more than once (there is one smoke pass; the smoke script's LLM call is acceptable).
6. Do NOT create branches/PRs. Do not amend or rewrite history. English commits.

## Constraints (unchanged)
Python 3.9 stdlib only; no pip; no pyobjc; graceful degradation everywhere; state file `.bot_state.json` gitignored.

## Final report (English)
Files changed; unittest count (N/N pass); smoke_e2e output summary; deviations; open questions.