# Research: Reading macOS Notifications Programmatically (macOS 12.7.6 Intel)

Date: 2026-10-24. All experiments verified live ON THIS MACHINE by the supervisor before this plan.

## Goal

Prototype a bot that reads macOS notifications (including phone-sourced ones) and
classifies/routes them with a local LLM. This document records the approaches
tested and the winner, with evidence. The implementation plan must build on the
WINNER approach only.

## Approved approaches considered and tested

### A. Notification Center SQLite database (db2) polling — WINNER, PROVEN

- Path: `$(getconf DARWIN_USER_DIR)/com.apple.notificationcenter/db2/db` (macOS ≤ 15).
  On this Mac: `/var/folders/8_/v6xpngy957110vxp43jq1k080000gn/0/com.apple.notificationcenter/db2/db`
- Readable as the plain user — **no TCC, no Accessibility, no Full Disk Access needed**.
  WAL mode enabled (db-wal present); safe read pattern = copy `db`, `db-wal`, `db-shm`
  to a temp dir, then open the copy (validated).
- Schema (verified): tables `app`, `dbinfo`, `displayed`, `requests`, `categories`,
  `delivered`, `record`, `snoozed`.
  - `record(rec_id INTEGER PRIMARY KEY, app_id INTEGER, uuid BLOB, data BLOB, request_date REAL, request_last_date REAL, delivered_date REAL, presented Bool, style INTEGER, snooze_fire_date REAL)`
  - `app(app_id INTEGER PRIMARY KEY, identifier VARCHAR, badge INTEGER NULL)` — app_id → bundle id map.
  - `data` blob = **binary plist**, keys seen live: `app` (bundle id), `date` (CoreData epoch 2001-01-01),
    `req` { `titl`, `subt`, `body`, `scat`, ... }, `styl`, `uuid`, `srce`. Parses with
    Python stdlib `plistlib.loads(blob, fmt=FMT_BINARY)` (validated via `plutil`).
- Live cycle test: `osascript -e 'display notification ... with title "BOT-TEST-2"'`
  → new `record` rows appeared in the copied DB in ~2–6 s (2 rows for 1 notification:
  record is written on receive and again on presentation/response — dedupe required).
- Rows are REMOVED from this DB when the user clears Notification Center (notifwd &
  NotificationHistory READMEs confirm) → bot must persist its own archive.
- `rec_id` is monotonically increasing → poll using `max(rec_id)` watermark.
- Latency measured: first insert ~2 s after post. Poll interval 2 s is adequate.
- Prior art using this approach: `notifwd` (jrmann100, Python, polls db2, forwards to Prowl),
  `notification_history` (birk5437, Swift, polls db2, needs FDA on newer macOS),
  "Roar" (tyler.io 2022, watches the NC db).

### B. AppleScript + System Events UI scrape — tested, REJECTED

`osascript 'tell application "System Events" to tell process "NotificationCenter" ...'`
hangs indefinitely without an Accessibility (TCC) grant for the host process (tested —
timed out at 120 s). Requires UI scripting permission + OS-version-fragile element paths;
only sees transient banners. Rejected: too much setup friction for end users, fragile.

### C. DistributedNotificationCenter observer — deprioritized

Carries per-app macOS-internal events, not user-app notification payloads
(HackTricks/SentinelOne docs). Probe compiled a Swift observer, but swiftc/toolchain
hung at compile on this machine twice (inconclusive). Not needed: A supersedes it.

### D. UNUserNotificationCenter observer in own app — documented, rejected

An app only receives ITS OWN notifications via this API; there is no supported way
to observe system-wide user notifications. Rejected by design.

### E. Off-the-shelf apps — not suitable for the bot

Roar (abandoned, display-only). NotificationHistory (requires macOS 14+, FDA, Swift 5.9).
notifwd is the closest functional relative (Python, stdlib, db2 polling, forwarder-only,
no AI, no routing, no persistence) — good behavioral reference, not a dependency.

### F. Phone (iPhone) notifications — constraint documented

- Native full iPhone→Mac notification mirroring requires **macOS Sequoia 15+
  (iPhone Mirroring)** — NOT available on this Mac (macOS 12.7.6 Monterey).
- On Monterey, iPhone-sourced content reaching NC: SMS/iMessage relay via Messages
  ("Text Message Forwarding"); those land in `record` as `com.apple.mobilesms`.
- No iPhone paired/nearby currently (checked via `system_profiler SPBluetoothDataType`).
- Design implication: the bot must treat phone mirroring as an OPTIONAL source —
  whatever lands in the NC database is captured; when the Mac runs a newer OS with
  iPhone Mirroring enabled, mirrored notifications flow through the same DB and the
  bot picks them up automatically (bundle ids of iOS apps appear in `app.identifier`).

## AI routing (verified)

- Local ollama: `http://127.0.0.1:11434`, model `glm-5.3-flash:cloud`, endpoint
  `/api/chat` with `stream: false` returns complete JSON reliably in ~2.4 s
  (measured live with a classification prompt; clean JSON output confirmed).
- Keep a strict JSON prompt + tolerant parsing (strip code fences).

## Constraints for implementation

- macOS 12.7.6 x86_64; system `python3` = 3.9.6 — stdlib only (sqlite3, plistlib,
  urllib, json, argparse, shutil, tempfile, logging). No pyobjc, no pip installs.
- Telegram routing optional: env `TG_BOT_TOKEN` + `TG_CHAT_ID` (or config values);
  skip gracefully when unset.
- Config: single `config.json`; log: JSONL.
- Human-friendly interactions for the end user matter: a one-command setup and a
  `--test-notify` helper that fires demo notifications.