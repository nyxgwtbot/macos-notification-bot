You are planning ONLY — do NOT implement, do NOT edit files, do NOT commit.
Analyze the repository (greenfield; only RESEARCH.md exists) and the mission below,
then produce a structured execution plan. The plan will be executed by another agent.

Repo: /Users/bot/repos/macos-notification-bot (currently contains only RESEARCH.md —
READ IT FIRST: it contains the verified research, the winning approach, schema facts,
and implementation constraints; plan strictly on the WINNER approach A, db2 polling).

## Mission to plan (MVP)

A single-package macOS (12.7.6, x86_64) prototype: "notification bot" that
watches the system Notification Center database, classifies each new
notification with a local LLM, routes by rule, and is easy for an end user to
run. Python 3.9 stdlib only (sqlite3, plistlib, urllib, json, argparse,
shutil, tempfile, logging); no pip installs; no pyobjc; no node.

Required deliverables:
1. bot.py — single-file CLI/daemon (stdlib only):
   - poll NC db2 every 2 s using the temp-copy pattern (copy db + db-wal +
     db-shm to a temp dir, open copy read-only); path resolved via
     `$(getconf DARWIN_USER_DIR)com.apple.notificationcenter/db2/db`
   - parse `record` rows with plistlib (binary plist of blob `data`), map
     app_id -> identifier via `app` table; dedupe on rec_id with a persisted
     watermark/state file (rows can repeat: one on receive, one on
     presentation; and rows vanish when the user clears NC — archive locally)
   - classify each new notification via ollama at http://127.0.0.1:11434
     model "glm-5.3-flash:cloud", endpoint /api/chat, stream false; strict
     JSON output {category: message|alert|system|promo|calendar|other,
     urgency: low|medium|high, summary: <=12 words}; tolerant parse (strip
     code fences); on failure fall back to category "other", urgency "low"
     and log the raw error; never crash the poll loop on LLM errors
   - route via rules from config.json evaluated top-down (first match wins):
     rule = {"when": {"category": ..,"urgency": .., "app": ..}, "actions":
     ["log" | "telegram" | "command:<path>"]}; default fallthrough "log"
   - "telegram" action posts via env TG_BOT_TOKEN/TG_CHAT_ID (or config
     telegram.token/chat_id overrides) using urllib; skip gracefully when
     unset; include app, title, subtitle, body and AI summary in the message
   - "command" action runs the configured program with the notification JSON
     as argv[1] argument (subprocess, timeout 30 s, never crashes loop)
   - JSONL event log (one line per event: received → classified → routed)
     under logs/ next to the repo; --test-notify helper posts demo
     notifications via osascript (3 categories to classify); --dump prints
     the most recent parsed records (read-only debug); also log a human
     readable line to stderr
   - robustness: tolerate WAL changes while copying (retry), tolerate corrupt
     plist, tolerate db path missing (clear error message), SIGTERM/SIGINT
     exit path that saves state and stops cleanly
2. config.json defaults: urgency high OR category message → log + telegram;
     promo/system → log only. Document every key.
3. README.md: what it does, how to run (one command), how to configure rules
   and routing, iPhone-notes section (Monterey: only SMS/iMessage relay via
   Messages arrives natively; full phone mirroring needs macOS Sequoia+ —
   when it lands, mirrored entries appear in the same db2 automatically),
   TCC/permissions: none required, troubleshooting (--dump).
4. tests/test_bot.py — unit tests for: temp-copy reader + plist parser
   (synthetic blob fixture embedded, parsed WITHOUT calling ollama), dedupe
   watermark logic, router table evaluation, telegram payload building
   (network mocked). Must pass with system python3 (3.9) via unittest.
5. scripts/smoke_e2e.py — end-to-end: starts the bot once mode, posts 2 demo
   notifications via osascript, waits, asserts the JSONL events show
   classification and routing (telegram action may be skipped when env
   missing — print SKIPPED, not failure).

Branch/PR/commit policy: the supervisor creates branches and commits as
separate steps; do NOT plan branch creation or PR submission steps.

Plan format (markdown):
## Objective
<goal + acceptance criteria>

## Steps (ordered)
1. <concrete step> — files touched, expected outcome
2. ...

## Dependencies
- step N depends on step M

## Risks / Open questions
- ...

Do NOT write code. Plan only. Keep to max 8 steps. English only.