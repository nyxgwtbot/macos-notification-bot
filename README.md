# macOS Notification Bot

A small, zero-dependency Python bot that reads macOS system notifications, classifies
them with a local LLM, and routes them by rules: to the event log, to Telegram, or to
external commands. Runs as a daemon or one-shot, with no special macOS permissions.

## Overview

The bot polls the macOS Notification Center SQLite database (`db2`) every couple of
seconds, parses the binary plists stored in each record, sends the text to a local
Ollama model for classification (category, urgency, one-line summary), matches the
result against a top-down rule list, and runs the actions of the first matching rule.

Key properties:

- **Stdlib only.** Python 3.9 (the stock `/usr/bin/python3` on macOS 12 works), no pip
  installs, no pyobjc, no private APIs.
- **Zero TCC prompts.** It never asks for Accessibility, Full Disk Access, or screen
  recording. See [Permissions (zero TCC)](#permissions-zero-tcc).
- **Graceful degradation.** If Ollama is unreachable or returns unparsable output, each
  notification still falls back to `category: "other"`, `urgency: "low"`, and a summary
  taken from the title/body (truncated to 120 characters). The bot keeps running.
- **Deduplication.** macOS writes a record when a notification is delivered and again
  when it is presented; the bot suppresses duplicates with a `rec_id` watermark plus a
  recent-UUID window, persisted atomically to `.bot_state.json`.

Requirements:

| Requirement | Notes |
| --- | --- |
| macOS 12 (Monterey) or newer | Developed and verified on macOS 12.7.6 x86_64 |
| Python 3.9+ | Stock `/usr/bin/python3`; standard library modules only |
| Ollama running locally | Default endpoint `http://127.0.0.1:11434`, model `glm-5.3-flash:cloud` |
| Telegram (optional) | Only if you use `telegram` actions; skipped cleanly when unconfigured |

## Architecture

Short version: poll -> parse -> dedupe -> classify -> route -> act.

```
Notification Center db2 (SQLite + binary plists)
  |  read-only snapshot: db / db-wal / db-shm copied to a temp dir
  v
Parse plist blob -> app / title / subtitle / body / presented / uuid
  v
Dedupe: rec_id watermark + recent-UUID window  (.bot_state.json)
  v
Classify via local Ollama (any error -> fallback: other / low / summary<=120)
  v
Route: top-down rules, first match wins
  v
Actions: log (events.jsonl) | telegram | command:<path> (JSON as argv[1], 30s timeout)
```

Details worth knowing:

- **Snapshotting.** Notification Center keeps the database in WAL mode and writes
  continuously. The bot does not open the live file; it copies `db`, `db-wal`, and
  `db-shm` to a temporary directory and opens the copy in read-only SQLite URI mode.
  A transient copy race (a WAL write during the copy) triggers one retry.
- **Watermark.** `rec_id` is monotonically increasing, so the bot only queries rows
  with `rec_id > last_rec_id`. Recent notification UUIDs are also tracked (up to 1000)
  to catch duplicates that reappear under a new row id.
- **State persistence.** `.bot_state.json` (`last_rec_id`, `recent_uuids`,
  `updated_at`) is written atomically (temp file + rename) after each processed
  notification, and saved on `SIGINT`/`SIGTERM` shutdown.
- **Local archive.** The system can purge Notification Center rows (for example, when
  you clear notification history). The bot's JSONL event log is the persistent record.
- **Config merge is shallow.** Any top-level key present in `config.json` replaces the
  built-in default entirely. If you supply an `ollama` object, specify all three keys
  (`url`, `model`, `timeout`); if you supply `telegram`, specify both `token` and
  `chat_id`; `rules` are replaced as a whole.

## Quick start

```bash
# 1. Verify Python (stock /usr/bin/python3 is fine)
python3 --version

# 2. Install Ollama (https://ollama.com) and pull the classification model
ollama pull glm-5.3-flash:cloud

# 3. Sanity check: post 3 demo notifications on your Mac
#    (Bank Security Alert, Alice (Messages), App Store)
python3 bot.py --test-notify

# 4. Prove the DB reader works: dump the 10 most recent Notification Center records
python3 bot.py --dump

# 5. Run one poll cycle end-to-end (classification + routing + logging)
python3 bot.py --once

# 6. Run as a daemon (default mode). Ctrl-C stops it cleanly.
python3 bot.py
```

After step 5 or 6, inspect `logs/events.jsonl` for the actual events.

- The daemon default requires a running local Ollama (`config.json` points at
  `http://127.0.0.1:11434/api/chat`). It still runs without Ollama, but every
  classification falls back (see [Troubleshooting](#troubleshooting)).
- A different config file: `python3 bot.py --config path/to/other.json` (default is
  `config.json` in the repo root).

Telegram is optional. To enable it, set the credentials in `config.json` or via
environment variables before starting the bot:

```bash
export TELEGRAM_BOT_TOKEN="123456:ABC..."   # token from @BotFather
export TG_CHAT_ID="123456789"               # chat that should receive the messages
python3 bot.py
```

When no credentials are configured, `telegram` actions are skipped with a logged
`skipped` status; this is not an error.

## CLI reference

| Flag | Effect |
| --- | --- |
| *(none)* | Run in daemon mode: poll forever at `poll_interval`, Ctrl-C to stop |
| `--once` | Run a single poll cycle, write logs, and exit (logs a `startup` event with `"mode": "once"`) |
| `--dump [N]` | Print the N most recent Notification Center records (default 10) and exit. No network: does not call Ollama or Telegram. Primary troubleshooting tool |
| `--test-notify` | Post 3 demo notifications via `osascript` and exit: "Bank Security Alert", "Alice (Messages)", "App Store" |
| `--config <path>` | Use this JSON config instead of `config.json` |
| `--db-path <path>` | Override the Notification Center `db2` database path (advanced) |
| `--watermark <int>` | Override the starting `last_rec_id` watermark for this run (advanced, see Troubleshooting) |

## Configuration

Default `config.json` (verbatim from the repo):

```json
{
  "poll_interval": 2.0,
  "state_file": ".bot_state.json",
  "log_file": "logs/events.jsonl",
  "ollama": {
    "url": "http://127.0.0.1:11434/api/chat",
    "model": "glm-5.3-flash:cloud",
    "timeout": 30.0
  },
  "telegram": {
    "token": "",
    "chat_id": ""
  },
  "rules": [
    {
      "name": "high_urgency",
      "condition": {
        "urgency": ["high"]
      },
      "actions": ["log", "telegram"]
    },
    {
      "name": "messages",
      "condition": {
        "category": ["message"]
      },
      "actions": ["log", "telegram"]
    },
    {
      "name": "promo_or_system",
      "condition": {
        "category": ["promo", "system"]
      },
      "actions": ["log"]
    },
    {
      "name": "default",
      "condition": {},
      "actions": ["log"]
    }
  ]
}
```

Configuration keys:

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `poll_interval` | float (seconds) | `2.0` | Daemon sleep between poll cycles |
| `state_file` | path | `.bot_state.json` | Dedup watermark + recent UUID window; created at runtime |
| `log_file` | path | `logs/events.jsonl` | JSONL event log (one JSON object per line; directory is created if missing) |
| `ollama.url` | URL | `http://127.0.0.1:11434/api/chat` | Classification endpoint (Ollama `POST /api/chat`, `stream: false`) |
| `ollama.model` | string | `glm-5.3-flash:cloud` | Model name the request must use |
| `ollama.timeout` | float (seconds) | `30.0` | HTTP timeout for one classification call |
| `telegram.token` | string | `""` | Bot token. Falls back to env `TELEGRAM_BOT_TOKEN` first, then `TG_BOT_TOKEN` |
| `telegram.chat_id` | string | `""` | Target chat. Falls back to the first entry of comma-split env `TELEGRAM_ALLOWED_USERS`, then `TG_CHAT_ID` |
| `rules` | list | see above | Ordered routing rules; first match wins |

Note on overrides: the merge is shallow. `{"ollama": {"model": "..."} }` would discard
the default `url` and `timeout`. When customizing `ollama.*`, list all three keys;
when customizing `telegram.*`, list both.

## Rules

Rules are evaluated top-down in the order they appear in `config.json`; the first rule
whose condition matches decides the extra actions. The local event log always records
the notification: the `event` line is appended unconditionally, whatever the rule's
`actions` list contains.

Condition semantics:

- Each condition key is looked up **against the classification first**, then against
  the notification record. A key that exists in neither means the rule cannot match.
- String comparison is case-insensitive.
- A list value means "matches any of these" (`"category": ["promo", "system"]` is an OR).
- An empty condition `{}` always matches; put such a catch-all rule last.
- No glob/wildcard or substring matching: values are compared for equality only.
- If no rule matches at all (for example, you removed the default rule), the bot
  applies a built-in fallthrough named `default_fallthrough` with `["log"]`.

Condition keys and where they come from:

| Key | Source | Possible values |
| --- | --- | --- |
| `category` | classification (LLM or fallback) | `message`, `alert`, `promo`, `system`, `other` |
| `urgency` | classification (LLM or fallback) | `high`, `medium`, `low` |
| `summary` | classification (LLM or fallback) | free text (1-sentence summary) |
| `app` | notification | bundle identifier, e.g. `com.apple.mobilesms` |
| `title` | notification | notification title text |
| `subtitle` | notification | notification subtitle text |
| `body` | notification | notification body text |
| `presented` | notification | `true` / `false` |

Available actions (a rule lists any combination; `telegram` always runs before any
`command:<path>` actions, regardless of the order they are listed in):

| Action | Behavior |
| --- | --- |
| `log` | Effectively a no-op marker: the `event` JSON line and the human-readable stderr line for every processed notification are written regardless of the rule's `actions`, and `action_results.log` is always `ok` |
| `telegram` | Send a formatted message via the Telegram Bot API; always executed before any `command:<path>` actions (listed order is not honored for telegram-vs-command); status is `ok`, `skipped` (no credentials), or `error: ...` |
| `command:<path>` | Run an external command with a JSON serialization of the event as `argv[1]` (not stdin). 30-second timeout; exit code 0 -> `ok`, non-zero -> `exit_<code>`, otherwise `timeout` / `error: ...` |

The default rules shipped in `config.json` are shown verbatim in the
[Configuration](#configuration) section above.

### Example: custom rule

To route banking app notifications to a local script, add a rule above the catch-all.
Both a condition on `app` and classification keys can coexist (all keys must match):

```json
{
  "name": "banking_alerts",
  "condition": {
    "app": ["com.example.bankapp"],
    "urgency": ["high", "medium"]
  },
  "actions": ["log", "command:/usr/local/bin/bank_alert.sh"]
}
```

The command action receives the full event JSON as its first argument:

```sh
#!/bin/sh
# /usr/local/bin/bank_alert.sh — event JSON arrives as $1
printf '%s\n' "$1" >> "$HOME/bank_alerts.log"
```

## Event log

`logs/events.jsonl` contains one JSON object per line. There are exactly two event
types.

Startup event:

```json
{"event": "startup", "timestamp": "...", "mode": "once|daemon", "watermark": N}
```

Notification event:

```json
{"event": "notification", "timestamp": "...", "rec_id": ..., "app": ..., "uuid": ...,
 "title": ..., "subtitle": ..., "body": ..., "classification": {"category": ..., "urgency": ..., "summary": ...},
 "rule_matched": ..., "actions": [...], "action_results": {...}}
```

An optional top-level `"telegram"` key appears only when a `telegram` action ran for
that notification; its value is `"ok"`, `"skipped"`, or `"error: ..."`.

Real sample line from this repo's log:

```json
{"event": "notification", "timestamp": "2026-10-25T01:31:37.966538+00:00", "rec_id": 3, "app": "com.if.Amphetamine", "uuid": "34d077aa-6f04-4433-a172-34370bae03ba", "title": "notif_amph_keep_awake", "subtitle": "Nova sessão ao abrir o app", "body": "notif_disp_sleep_state notif_state_prevented | notif_scrn_svr_state notif_state_prevented | notif_cdm_state notif_state_prevented", "classification": {"category": "system", "urgency": "low", "summary": "Amphetamine session active: display sleep, screensaver, and CDM states are prevented until the app session."}, "rule_matched": "promo_or_system", "actions": ["log"], "action_results": {"log": "ok"}}
```

Reminder: the system may delete Notification Center database rows when you clear
notification history, so treat this file as the bot's durable archive of what it saw.

## Troubleshooting

1. **Is the DB read working?** Run `python3 bot.py --dump` (or `--dump 25` for more
   entries; the default is 10). This prints recent Notification Center records directly
   from the database snapshot and does not touch the network. If this fails, the bot
   cannot read the database at all (wrong macOS layout; try `--db-path`).

2. **Check what the bot actually did.** Read `logs/events.jsonl`. Every processed
   notification is recorded there with its classification, matched rule, and action
   results. The `log` action also prints a readable line to stderr as it runs.

3. **Is Ollama running with the model?**

   ```bash
   curl http://127.0.0.1:11434/api/tags   # should list glm-5.3-flash:cloud
   ```

4. **Everything lands as `category: "other"` / `urgency: "low"`.** That is the graceful
   fallback, which triggers when classification fails to connect, times out, or returns
   unparsable output (the summary is then the title/body, truncated to 120 chars). It
   means the bot is healthy but Ollama is not. Check step 3 and the model name in
   `config.json`. The bot never crashes because of the classifier.

5. **Duplicates or missing history — reset the state.** `.bot_state.json` stores the
   `last_rec_id` watermark and recent UUIDs. Every record at or below the watermark is
   suppressed. To re-scan older notifications still present in the database:

   ```bash
   # stop the bot, then either:
   rm .bot_state.json                 # full reset; next run processes everything still in db2
   # or keep the file and just rescan from a chosen point, for one run:
   python3 bot.py --watermark 0 --once    # advanced: explicit watermark override
   ```

6. **Telegram shows `skipped` in the log.** No token/chat_id could be resolved. The
   precedence is `config.json telegram.token` > `TELEGRAM_BOT_TOKEN` > `TG_BOT_TOKEN`,
   and `config.json telegram.chat_id` > first entry of `TELEGRAM_ALLOWED_USERS` >
   `TG_CHAT_ID`. Unconfigured Telegram is a clean skip, not a failure.

## Permissions (zero TCC)

The bot reads only the Notification Center database, which is owned by your user, via
read-only file copies into a temp dir. As documented in RESEARCH.md (approach A,
verified live), it runs as the plain user with:

- no Accessibility grant,
- no Full Disk Access,
- no screen-recording or automation permissions,
- no pyobjc, no private APIs, and no system-wide code injection.

Alternative approaches were tested and rejected for exactly this reason: UI scraping of
the Notification Center applet via System Events hangs without an Accessibility grant
and only sees transient banners (RESEARCH.md approach B), and in-process
`UNUserNotificationCenter` observers only ever receive an app's own notifications
(approach D). The `db2` snapshot approach keeps the user at zero permission prompts.

## iPhone & SMS notes

SMS and iMessage notifications from your iPhone appear on the Mac because of Messages
relay ("Text Message Forwarding"): on macOS Monterey, notifications from iPhone
SMS/iMessage show up under Messages. That is the limit of what the Mac sees of the
phone locally.

FULL mirroring of arbitrary iPhone apps requires macOS Sequoia+ (iPhone Mirroring),
which is not available on Monterey. For this pipeline, however, the distinction is
transparent: entries — Monterey-era relayed SMS/iMessage under
`com.apple.mobilesms`, or Sequoia-era mirrored notifications with iOS bundle ids —
all land in the same `db2` database automatically, and the bot picks them up like any
other notification.

On Monterey, expect phone-originated SMS/iMessage notifications to reach `db2` through
the Messages relay only; other iPhone apps will not appear until you run a newer macOS
with iPhone Mirroring.

## Tests & smoke

Offline unit tests (no Ollama, no Telegram, no live Notification Center writes; uses a
synthetic binary plist fixture):

```bash
python3 -m unittest discover -s tests
```

End-to-end smoke test:

```bash
python3 scripts/smoke_e2e.py
```

What it does: posts 2 demo notifications via `osascript`, runs `bot.py --once`, and
verifies the pipeline evidence (ingestion, classification, routing) in
`logs/events.jsonl`. A Telegram result of `SKIPPED` (because credentials are not set in
your environment) is acceptable and is not a failure. The script is idempotent: on
reruns it reuses prior evidence and does not post or classify again.

## Project layout

```
bot.py                  Everything: db2 reader, plist parser, state manager, classifier,
                        rule router, action dispatcher, CLI
config.json             Runtime configuration (polling, Ollama, Telegram, rules)
tests/                  Offline unit suite:
                          python3 -m unittest discover -s tests
scripts/
  smoke_e2e.py          End-to-end smoke test:
                          python3 scripts/smoke_e2e.py
PLAN.md                 Implementation plan (steps, acceptance criteria, risk notes)
RESEARCH.md             Architecture findings: approach evaluation, zero-TCC rationale,
                        Monterey vs Sequoia sections, AI routing notes
.bot_state.json         Runtime state (watermark + recent UUIDs) — gitignored
logs/
  events.jsonl          Runtime event log (JSONL) — gitignored; created by the bot
```

The two runtime artifacts, `.bot_state.json` and `logs/`, are generated in place and
listed in `.gitignore`; do not commit them.