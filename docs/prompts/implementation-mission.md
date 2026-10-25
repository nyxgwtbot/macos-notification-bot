# MISSION: Implement macOS Notification Bot MVP (per PLAN.md)

## EXECUTION MODE — You are a SINGLE executor. No sub-agents exist. Do not narrate
coordination, never report to anyone else, never wait for any other agent — run
every step yourself and commit your work.

## Context
- Repo: THIS repo (greenfield). Existing files: RESEARCH.md (verified facts — READ FIRST), PLAN.md (the plan to execute), nothing else.
- macOS 12.7.6 x86_64 host. `python3` = 3.9.6 system CLT python. Stdlib only. No pip installs. No pyobjc. No node/npm needed.
- The mission is fully specified in PLAN.md (repo root). Follow its steps in order.
- Commits: commit with a clear English message after EACH completed step (intermediate commits protect work; supervisor handles branch/PR only — do not create branches or PRs).
- Final commit must leave the repo clean working tree with all deliverables.

## Corrections to PLAN (these override any conflict)
1. Telegram env vars: read `TELEGRAM_BOT_TOKEN` and use chat default from `TELEGRAM_ALLOWED_USERS` (comma-separated; take the FIRST id) — these exact names; also accept legacy aliases `TG_BOT_TOKEN`/`TG_CHAT_ID`. Config keys `telegram.token` / `telegram.chat_id` override env. Missing token => telegram action logs SKIPPED (event JSONL line with "telegram":"skipped"), never an error.
2. The `app` table lookup: cache identifier per app_id at first sight of the id.
3. Dedupe: persisted state file `.bot_state.json` (as PLAN says) next to the script (repo-local, gitignored); watermark = max(rec_id) seen; also keep a small set of recent uuids to absorb double-rows (record+presentation).
4. On macOS 12 the db2 path is exactly `$(getconf DARWIN_USER_DIR)com.apple.notificationcenter/db2/db` (trailing slash in DARWIN_USER_DIR). Read via temp copy of db+db-wal+db-shm (shutil.copy2); open copy with sqlite3 in read-only URI mode. Retry copy once on sqlite error. NEVER open the live db directly for long-lived connections.
5. plistlib.loads(blob, fmt=plistlib.FMT_BINARY); record fields: data["app"] (bundle id), data["date"] (epoch 2001), data["req"]["titl"/"subt"/"body"]. Missing keys => empty string, never KeyError crash.
6. ollama client: urllib POST to http://127.0.0.1:11434/api/chat, body includes "stream": false; model "glm-5.3-flash:cloud" (config-overridable). Timeout 30s. Tolerant JSON parse (strip ``` fences, find first {...} block).
7. Router: rules list from config.json, first match wins, fallthrough action ["log"].
8. Tests must run green with `python3 -m unittest discover -s tests -v` on system python3 9.6. NO network in unit tests (telegram mocked, ollama not called).

## Acceptance criteria (verify yourself, evidence in final report)
- `python3 -m unittest discover -s tests -v` exits 0.
- `python3 bot.py --dump` prints recent parsed notifications (title/body/app) without requiring network.
- `python3 bot.py --once` starts, connects the real db, exits 0 with an events JSONL log line written even if zero new notifications (log a "startup" event).
- LLM classification produces valid JSON for both fixtures (unit test with a stubbed HTTP handler, no live network).
- All plan checklist items of PLAN.md addressed or explicitly listed as open questions in the final report.

## Final report (English)
State: files created, test results (number of tests, pass count), any deviations from PLAN and why, open questions for the supervisor.