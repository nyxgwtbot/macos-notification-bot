## Objective
- Build a lightweight, zero-dependency macOS (12.7.6 x86_64) notification bot prototype using Python 3.9 standard library that polls Notification Center `db2`, classifies incoming notifications with local Ollama (`glm-5.3-flash:cloud`), routes them by top-down rules (log, Telegram, external command), and provides full CLI utilities.
- Acceptance criteria:
  1. `bot.py` operates using Python 3.9 stdlib only (zero pip dependencies, zero pyobjc).
  2. NC db2 reader successfully copies `db`, `db-wal`, `db-shm` to a temp directory, decodes binary plists, extracts app/title/subtitle/body, and dedupes on `rec_id`.
  3. Ollama classification endpoint (`/api/chat`, `glm-5.3-flash:cloud`) returns structured JSON (`category`, `urgency`, `summary`), tolerates markdown code fences, and falls back to `category: "other"`, `urgency: "low"` without crashing on network or parsing failure.
  4. Routing engine evaluates `config.json` rules top-down, correctly dispatching `log`, `telegram` (via `urllib`, graceful skip when credentials missing), and `command:<path>` (subprocess timeout 30s).
  5. JSONL event logs are persisted under `logs/events.jsonl`, `--dump` outputs recent records, and `--test-notify` posts 3 test notifications via `osascript`.
  6. Unit test suite `tests/test_bot.py` passes under `python3 -m unittest` with mocked network and synthetic binary plists.
  7. Smoke test `scripts/smoke_e2e.py` verifies end-to-end receipt, classification, and routing.
  8. `README.md` documents setup, configuration, Monterey SMS/iMessage relay vs Sequoia iPhone Mirroring, and troubleshooting.

## Steps (ordered)
1. **Define Default Configuration and Rules Schema (`config.json`)** — files touched: `config.json`. Expected outcome: Valid JSON configuration containing defaults for `poll_interval` (2.0s), `state_file` (`.bot_state.json`), `log_file` (`logs/events.jsonl`), `ollama` settings (`http://127.0.0.1:11434`, model `glm-5.3-flash:cloud`), `telegram` placeholders, and top-down routing rules: high urgency OR message category -> `["log", "telegram"]`; promo or system category -> `["log"]`; default fallthrough -> `["log"]`.
2. **Implement Notification Center DB Snapshotting and Plist Parser (`bot.py`)** — files touched: `bot.py`. Expected outcome: Path resolution via `getconf DARWIN_USER_DIR` to locate `com.apple.notificationcenter/db2/db`; temporary directory snapshot routine copying `db`, `db-wal`, `db-shm` with WAL retry handling; SQLite reader opening read-only URI; binary plist decoding via `plistlib.loads` extracting app, title, subtitle, body, and timestamp; and CLI flags `--dump [N]` and `--test-notify` (via `osascript`).
3. **Implement Watermark Deduplication and Local Archival (`bot.py`)** — files touched: `bot.py`. Expected outcome: High-watermark tracking (`last_rec_id`) and UUID tracking stored in `.bot_state.json` to prevent re-processing presentation-stage duplicates; local history retention before system purges; and signal handling (`SIGINT`, `SIGTERM`) for atomic state persistence on shutdown.
4. **Implement Local LLM Classifier with Tolerant Fallback (`bot.py`)** — files touched: `bot.py`. Expected outcome: HTTP POST client using `urllib.request` against Ollama `/api/chat` with model `glm-5.3-flash:cloud`; strict JSON prompt enforcing `{category, urgency, summary}`; code fence stripping and JSON parsing; and non-fatal fallback to `{category: "other", urgency: "low", summary: "<title>"}` upon connection or parsing errors.
5. **Implement Rule-Based Routing, Actions, and JSONL Logging (`bot.py`)** — files touched: `bot.py`. Expected outcome: Top-down rule evaluation engine; `log` action appending to `logs/events.jsonl` and writing readable lines to `stderr`; `telegram` action posting via Bot API with graceful skipping when `TG_BOT_TOKEN`/`TG_CHAT_ID` are unset; `command:<path>` action running external binaries with JSON in `argv[1]` (timeout 30s); and main daemon loop coordinating reader, classifier, router, and state update with `--once` support.
6. **Create Unit Test Suite (`tests/test_bot.py`)** — files touched: `tests/test_bot.py`. Expected outcome: Offline test suite passing with system `python3 -m unittest discover -s tests`, covering temp-copy reading and plist parsing with an embedded synthetic plist fixture, watermark deduplication, top-down routing precedence, Telegram payload construction and missing-env skipping, and classifier fallback handling.
7. **Implement End-to-End Smoke Test Script (`scripts/smoke_e2e.py`)** — files touched: `scripts/smoke_e2e.py`. Expected outcome: Verification script that posts 2 notifications via `osascript`, runs `bot.py --once`, verifies record ingestion, LLM classification, and rule routing in `logs/events.jsonl`, verifies Telegram action cleanly outputs `SKIPPED` when unconfigured, and reports execution status.
8. **Write User Documentation (`README.md`)** — files touched: `README.md`. Expected outcome: Comprehensive guide detailing architecture (db2 polling + local LLM), one-command startup (`python3 bot.py`), configuration options, zero-TCC permissions model, Monterey SMS/iMessage relay vs Sequoia iPhone Mirroring compatibility, and troubleshooting with `--dump`.

## Dependencies
- Step 2 depends on Step 1 (DB reader consumes configuration defaults).
- Step 3 depends on Step 2 (Deduplication wraps parsed record stream).
- Step 4 depends on Step 2 (Classifier processes parsed notification payload).
- Step 5 depends on Steps 1, 2, 3, and 4 (Routing daemon orchestrates reader, dedupe, classifier, actions, and logger).
- Step 6 depends on Steps 1, 2, 3, 4, and 5 (Unit tests validate all `bot.py` modules).
- Step 7 depends on Step 5 (Smoke test executes `bot.py` CLI).
- Step 8 depends on Steps 1 through 7 (Documentation covers final implementation and CLI flags).

## Risks / Open questions
- **SQLite WAL Concurrency**: Notification Center continuously updates `db-wal`. Copying files while SQLite writes can cause transient lock or file integrity errors. *Mitigation*: The copy routine will implement exponential backoff retry and open copied SQLite connections with `mode=ro` and `immutable=1`.
- **Ollama Latency and Availability**: Network timeout or high system load during Ollama `glm-5.3-flash:cloud` inference (~2.4s latency measured) could stall the loop. *Mitigation*: Bounded HTTP timeout (10s) and non-crashing fallback to default classification (`other`/`low`).
- **Notification Presentation Duplication**: macOS writes records both upon delivery and upon banner presentation. *Mitigation*: Deduplicate using unique notification UUID and `rec_id` watermark stored in `.bot_state.json`.
- **Telegram Token Availability**: In headless or test environments, Telegram tokens are not configured. *Mitigation*: Actions engine and smoke tests explicitly treat missing tokens as a clean `SKIPPED` status rather than a failure.
