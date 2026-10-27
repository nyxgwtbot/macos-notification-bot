# macOS Notification Bot

A small, zero-dependency Python bot that reads macOS system notifications, classifies
them with a local LLM, and routes them in **two stages** — deterministic rules first,
then an LLM-based action router (Router v2) that picks among a configurable action
registry (event log, Telegram, finance curation, external commands). Includes a
**finance tracking plugin** that extracts Brazilian bank-notification transactions
(`R$ 1.234,56`-style amounts) into a local SQLite ledger and renders HTML reports with
inline SVG charts and PT-BR insights. Runs as a daemon or one-shot, with no special
macOS permissions.

## Overview

The bot polls the macOS Notification Center SQLite database (`db2`) every couple of
seconds, parses the binary plists stored in each record, sends the text to a local
Ollama model for classification (category, urgency, one-line summary), routes the
result in two stages, and runs the chosen actions:

1. **Deterministic rules** (`config.json` `rules`) are evaluated top-down; the first
   match wins and overrides everything else (backward compatible with Phase 1).
2. **Non-matching notifications** go to the LLM router, which sees only the *enabled*
   actions of the registry (`id` + `match_hint`) and answers
   `{"action_id": ..., "confidence": ..., "payload": ...}`. Decisions with confidence
   below `router.min_confidence` (default `0.5`), unknown ids, or any LLM failure fall
   back to the `log` builtin.

Key properties:

- **Stdlib only.** Python 3.9 (the stock `/usr/bin/python3` on macOS 12 works), no pip
  installs, no pyobjc, no private APIs — the HTML reports draw charts with hand-rolled
  inline SVG (no matplotlib).
- **Zero TCC prompts.** It never asks for Accessibility, Full Disk Access, or screen
  recording. See [Permissions (zero TCC)](#permissions-zero-tcc).
- **Graceful degradation.** If Ollama is unreachable or returns unparsable output, each
  notification still falls back to `category: "other"`, `urgency: "low"`, and a summary
  taken from the title/body (truncated to 120 characters); the router falls back to the
  `log` action. The bot keeps running.
- **Deduplication.** macOS writes a record when a notification is delivered and again
  when it is presented; the bot suppresses duplicates with a `rec_id` watermark plus a
  recent-UUID window, persisted atomically to `.bot_state.json`.
- **Idempotent finance ledger.** Curated bank transactions are stored by `rec_id`
  (`INSERT OR IGNORE`), so re-processing never duplicates a row; all monetary math
  uses `decimal.Decimal`.

Requirements:

| Requirement | Notes |
| --- | --- |
| macOS 12 (Monterey) or newer | Developed and verified on macOS 12.7.6 x86_64 |
| Python 3.9+ | Stock `/usr/bin/python3`; standard library modules only |
| Ollama running locally | Default endpoint `http://127.0.0.1:11434`, model `glm-5.3-flash:cloud` |
| Telegram (optional) | Only if you use `telegram` actions; skipped cleanly when unconfigured |

## Architecture

Short version: poll -> parse -> dedupe -> classify -> route (rules, then LLM) -> act
(+ finance plugin on money notifications).

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
Route (two stages):
  1. config.rules top-down, first match wins          -> layer "rule"
  2. LLM router over enabled actions (id+match_hint)  -> layer "llm_router"
     (low confidence / unknown id / LLM error -> "log" builtin
      with layer "fallback_in_fallback")
  v
Actions: log | telegram | finance | command:<path> / registry "command" actions
             |                |
      events.jsonl     FinanceCurator -> FinanceLedger (finance.db, idempotent)
                                |
                        `bot.py report` -> reports/<YYYY-MM>.html (inline SVG)
                                        -> markdown summary (+ optional Telegram)
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
  `chat_id`; `rules`, `actions`, `router` and `finance` are replaced as a whole.

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

# 6. Generate the finance report from the local ledger (see Finance tracking)
python3 bot.py report                # current month
python3 bot.py report --month 2026-10

# 7. Run as a daemon (default mode). Ctrl-C stops it cleanly.
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
| `report` | Subcommand: `python3 bot.py report [--month AAAA-MM]` — writes `reports/<month>.html`, prints the PT-BR markdown summary, and optionally sends it via Telegram (`finance.telegram_summary`) |
| `--month <AAAA-MM>` | Month of the `report` subcommand (default: current month); invalid months exit with code 2 |

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
  "router": {
    "min_confidence": 0.5,
    "timeout": 15.0
  },
  "actions": [
    {
      "id": "log",
      "match_hint": "Ação padrão: apenas registrar a notificação no histórico, sem nenhuma consequência externa.",
      "type": "builtin",
      "enabled": true,
      "config": {}
    },
    {
      "id": "telegram",
      "match_hint": "Enviar alerta pessoal no Telegram: mensagens diretas de contatos, urgências altas e avisos importantes que merecem atenção imediata.",
      "type": "builtin",
      "enabled": true,
      "config": {}
    },
    {
      "id": "finance",
      "match_hint": "Notificações bancárias e de finanças pessoais para curadoria de gastos: compras aprovadas, Pix enviado ou recebido, pagamentos, débitos, faturas, estornos e qualquer notificação com valores em R$.",
      "type": "builtin",
      "enabled": true,
      "config": {}
    },
    {
      "id": "example_command",
      "match_hint": "Exemplo de ação externa: executar um programa local com o payload da notificação em argv[1]. Desabilitada por padrão.",
      "type": "command",
      "enabled": false,
      "config": {
        "command": "/path/to/prog"
      }
    }
  ],
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
    }
  ],
  "finance": {
    "enabled": true,
    "ledger_db": "finance.db",
    "bank_apps": [],
    "telegram_summary": false,
    "categories": [
      "alimentacao",
      "transporte",
      "compras",
      "saude",
      "lazer",
      "servicos",
      "assinaturas",
      "outros"
    ]
  }
}
```

Configuration keys:

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `poll_interval` | float (seconds) | `2.0` | Daemon sleep between poll cycles |
| `state_file` | path | `.bot_state.json` | Dedup watermark + recent UUID window; created at runtime |
| `log_file` | path | `logs/events.jsonl` | JSONL event log (one JSON object per line; directory is created if missing) |
| `ollama.url` | URL | `http://127.0.0.1:11434/api/chat` | Classification/routing endpoint (Ollama `POST /api/chat`, `stream: false`) |
| `ollama.model` | string | `glm-5.3-flash:cloud` | Model name every LLM request must use |
| `ollama.timeout` | float (seconds) | `30.0` | HTTP timeout for one classification call |
| `telegram.token` | string | `""` | Bot token. Falls back to env `TELEGRAM_BOT_TOKEN` first, then `TG_BOT_TOKEN` |
| `telegram.chat_id` | string | `""` | Target chat. Falls back to the first entry of comma-split env `TELEGRAM_ALLOWED_USERS`, then `TG_CHAT_ID` |
| `router.min_confidence` | float 0–1 | `0.5` | Router v2 decisions below this confidence fall back to `log` |
| `router.timeout` | float (seconds) | `15.0` | Strict HTTP timeout for one router/finance-extraction/insights call |
| `actions` | list | see above | Extensible action registry consumed by the LLM router (details below) |
| `rules` | list | see above | Ordered deterministic rules evaluated BEFORE the LLM router |
| `finance.enabled` | bool | `true` | Master switch for the finance plugin |
| `finance.ledger_db` | path | `finance.db` | SQLite ledger path (created at runtime) |
| `finance.bank_apps` | list of strings | `[]` | Allowlist of app/bundle substrings; **empty = ingest from every app** the router decides is finance-related |
| `finance.telegram_summary` | bool | `false` | When true, `bot.py report` also sends the text summary via Telegram |
| `finance.categories` | list | 8 categories above | Category vocabulary for curation and reports; unknown categories map to `outros` |

Note on overrides: the merge is shallow. `{"ollama": {"model": "..."} }` would discard
the default `url` and `timeout`. When customizing `ollama.*`, list all three keys;
when customizing `telegram.*`, list both; when customizing `router`/`finance`/`actions`,
include the keys you care about plus the defaults you want to keep.

## Rules

Rules are the **first routing stage** and are evaluated top-down in the order they
appear in `config.json`; the first rule whose condition matches decides the actions and
skips the LLM router entirely. The local event log always records the notification: the
`event` line is appended unconditionally, whatever the rule's `actions` list contains.

Condition semantics:

- Each condition key is looked up **against the classification first**, then against
  the notification record. A key that exists in neither means the rule cannot match.
- String comparison is case-insensitive.
- A list value means "matches any of these" (`"category": ["promo", "system"]` is an OR).
- An empty condition `{}` always matches. **The shipped config has no catch-all rule
  on purpose** so unmatched notifications flow into the LLM router. If you add one,
  put it last and be aware it will suppress Router v2.
- No glob/wildcard or substring matching: values are compared for equality only.
- If no rule matches, the notification goes to **Router v2** (LLM router). When the
  router cannot decide either (low confidence, unknown id, Ollama down), the built-in
  fallthrough routes to `["log"]`.

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
| `finance` | Curate the notification as a bank transaction (regex first, LLM fallback extraction) and store it in `finance.db`; executed only when registered by the finance plugin (i.e. `finance.enabled` is true) |
| `command:<path>` | Legacy Phase 1 form: run an external command with a JSON serialization of the event as `argv[1]` (not stdin). 30-second timeout; exit code 0 -> `ok`, non-zero -> `exit_<code>`, otherwise `timeout` / `error: ...` |
| registry action ids | Any id from the `actions` registry works in rules too; registry entries of type `command` run `config.command` with the event JSON in `argv[1]` |

## Router v2 (LLM actions)

When no deterministic rule matches, the notification is decided by the LLM router
(`notifybot/router.py`). Everything is explainable in the event log:

- The prompt contains **only the enabled actions'** `id` + `match_hint` (both in
  PT-BR), the notification fields, and its classification. Disabled or unknown actions
  are never offered and never executed.
- The model answers the JSON `{"action_id": "...", "confidence": 0.0-1.0,
  "payload": {...}}`. Parsing is tolerant (markdown fences, stray text, confidence
  given as a percentage).
- Decisions with `confidence < router.min_confidence` or with a non-registered id fall
  back to the `log` builtin.
- Every notification event line records the routing **layer**:
  - `"rule"` — a deterministic rule matched,
  - `"llm_router"` — the LLM chose a registered action,
  - `"fallback_in_fallback"` — the router itself fell back to `log`
    (`confidence` + the failure `reason` in the LLMRouter decision are kept in the
    event's `confidence` / router payload fields).

### Example: registering an action

```json
{
  "id": "example_command",
  "match_hint": "Exemplo de ação externa: executar um programa local com o payload da notificação em argv[1].",
  "type": "command",
  "enabled": true,
  "config": { "command": "/usr/local/bin/meu_script.sh" }
}
```

The `match_hint` is the contract with the LLM: describe, in PT-BR, *which
notifications belong to this action*. The LLM reads only the hints, so a good hint is
worth ten rules.

## Finance tracking (curadoria de gastos)

The `finance` action turns bank/spending notifications into a local ledger — the
repo's default registry ships the owner's example: *"notificações bancárias →
curadoria de gastos"*.

Pipeline (`notifybot/finance.py`):

1. **Regex-first extraction** of Brazilian amounts across `title`, `subtitle` and
   `body`: `R$ 1.234,56`, `R$ 1234,56`, `R$ 1234.56`, `R$ 50,00`, `R$ 45`,
   `R$ 1.234` (thousands), and bare decimals (`compra de 30,00`). Currency-prefixed
   values win over bare ones; integers without separators (`final 1234`) are ignored.
2. **Transaction kind** via Brazilian banking keywords: `gasto` (compra aprovada, Pix
   enviado, débito, fatura...), `recebimento` (Pix recebido, estorno, reembolso...),
   `outro` otherwise. `recebimento` is checked first so "pagamento recebido" is
   classified as income.
3. **Category** mapped to the configured vocabulary (`alimentacao`, `transporte`,
   `compras`, `saude`, `lazer`, `servicos`, `assinaturas`, `outros`) with keyword
   heuristics (iFood → `alimentacao`, "Mercado Livre" → `compras`, Netflix →
   `assinaturas`...); unknown categories fall back to `outros`.
4. **LLM fallback extraction** when the regex finds no amount: a strict-timeout
   Ollama call returns `{amount, kind, category, description}`; failures skip cleanly
   (`no_amount` / `invalid_amount` — no row is stored).
5. **Ledger** (`finance.db`, SQLite): `expenses(rec_id PRIMARY KEY, ts, bundle,
   amount REAL, kind, category, description, raw_json)`. Insertion is idempotent by
   `rec_id` (`INSERT OR IGNORE`); `ts` is ISO-8601 in **local time**; all aggregation
   uses `decimal.Decimal`.
6. **Event log evidence**: the notification line gains a `finance` object with
   `status`, `method` (`regex` / `llm`), `stored` (`inserted` / `ignored`),
   `amount`, `kind`, `category` — this is what `scripts/smoke_finance.py` verifies.

### Report command

```bash
python3 bot.py report               # current month
python3 bot.py report --month 2026-10
```

Writes `reports/<AAAAMM>.html` and prints a PT-BR markdown summary to stdout. The HTML
report contains, all as hand-rolled inline SVG (no external libs):

- **Gastos por categoria** — horizontal bar chart with BRL value labels;
- **Distribuição por categoria** — donut chart with percentage legend;
- **Evolução diária (acumulado)** — cumulative daily spend line;
- **Insights** — 3 to 5 PT-BR bullets seeded with computed stats (dominant category,
  biggest expense, busiest day, month-over-month delta when the previous month has
  data), generated via Ollama; a deterministic PT-BR fallback keeps the section alive
  when the LLM is unreachable;
- **Transações** — full table of the month (date, amount, kind, category,
  description).

Semantics: **"Total gasto" = `gasto` + `outro` rows**; `recebimento` rows stay in the
ledger and the transaction table (and appear as "Recebimentos: +R$ ...") but are
excluded from expense charts. With `finance.telegram_summary: true`, the markdown
summary is also sent to Telegram (plain text; missing credentials are a clean skip).

The default rules shipped in `config.json` are shown verbatim in the
[Configuration](#configuration) section above.

### Example: custom rule

To route banking app notifications to a local script deterministically, add a rule at
the top of the list. Both a condition on `app` and classification keys can coexist
(all keys must match):

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
 "rule_matched": ..., "layer": "rule|llm_router|fallback_in_fallback", "confidence": ...,
 "router_payload": {...}, "actions": [...], "action_results": {...}}
```

- `layer` records which routing stage decided: `"rule"` (a deterministic rule
  matched; `rule_matched` holds its name), `"llm_router"` (the LLM chose a registered
  action, with `confidence` in `0..1`), or `"fallback_in_fallback"` (the router itself
  fell back to the `log` builtin — `confidence` may still carry the LLM's value, and
  the router payload keeps the failure reason).
- `rule_matched` is `"none"` when no deterministic rule matched.
- An optional top-level `"telegram"` key appears only when a `telegram` action ran for
  that notification; its value is `"ok"`, `"skipped"`, or `"error: ..."`.
- An optional top-level `"finance"` key appears when a finance action ran; it carries
  `status` (`ok`/`skipped`), `method` (`regex`/`llm`), `stored` (`inserted`/`ignored`),
  `rec_id`, `amount`, `kind`, `category` and `description`.

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

7. **Everything routes to `log` with layer `fallback_in_fallback`.** The Router v2 LLM
    call failed, timed out (`router.timeout`, default 15 s), or the confidence was
    below `router.min_confidence`. Check `curl http://127.0.0.1:11434/api/tags`.

8. **No report after `bot.py report`, or empty insights.** The `finance` plugin only
    stores rows for notifications the router decided are finance-related. Verify that
    `finance.enabled` is `true`, that bank notifications actually carry `R$` values
    (regex needs a decimal separator for bare amounts), and that `finance.db` exists
    after a fresh `--once`. Ollama-unreachable runs still produce the report with
    deterministic PT-BR insights.

9. **Reset the finance ledger.** Rows are keyed by `rec_id`, so re-processing a
    notification never duplicates. To start over, stop the bot and delete the SQLite
    file (`rm finance.db` plus `-wal`/`-shm` siblings if present) — it is gitignored.

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

End-to-end smoke tests:

```bash
python3 scripts/smoke_e2e.py      # Phase 1 pipeline: ingest -> classify -> route -> log
python3 scripts/smoke_finance.py  # Phase 2 finance: 3 synthetic bank notifications ->
                                  #   ledger rows in finance.db -> reports/<month>.html
```

`scripts/smoke_e2e.py`: posts 2 demo notifications via `osascript`, runs `bot.py --once`,
and verifies the pipeline evidence (ingestion, classification, routing) in
`logs/events.jsonl`. A Telegram result of `SKIPPED` (because credentials are not set in
your environment) is acceptable and is not a failure. The script is idempotent: on
reruns it reuses prior evidence and does not post or classify again.

`scripts/smoke_finance.py`: posts 3 synthetic bank notifications with distinct `R$`
amounts via `osascript`, runs `bot.py --once` (bounded to 2 runs; db2 rows settle for
~2-6 s), verifies each marker has routed evidence (`layer` field) plus a matching
`finance.db` row, generates the monthly report via `bot.py report`, and asserts the
HTML contains 3+ `<svg>` elements, an `Insights` section, and the marker amounts.
Idempotent exactly like the e2e smoke: complete evidence (routed + finance event +
ledger row) means the marker is never re-posted, so reruns consume zero LLM calls and
never regenerate the report unless it is missing.

Python API smoke (sandboxed; never touches the real config/log/db):

```bash
python3 scripts/smoke_api.py                               # core rows
NOTIFYBOT_SMOKE_AGENT_MOCK=1 python3 scripts/smoke_api.py   # + agent pipeline rows (mock LLM)
```

## Desktop UI

An Ionic/Vue desktop app under `desktop/` (renderer) + `desktop/electron/` (Electron
wrapper) that operates the bot locally through `python3 bot.py api` — a stdlib-only,
loopback-bound JSON server (`notifybot/api.py`). Screens: **Dashboard** (daemon/health
+ live event feed), **Finance** (month navigator, category bars, ledger, report
generation + open/reveal), **Chat** (in-app agent + tool pipeline, below),
**Settings** (Ollama/router editing, action registry, rules with raw-JSON mode;
Telegram is read-only env-driven), **Logs** (raw tail with malformed lines dimmed).
No new Python dependencies; the renderer dependency set mirrors the HydraNote
scaffold minus duckdb/tiptap/pdf/native modules.

```bash
# First install:
cd desktop && npm ci && npm ci --prefix electron   # electron download may take a while

# Full chain: renderer build -> cap sync -> electron tsc:
cd desktop && npm run desktop:build

# Run the window:
cd desktop/electron && npm start
# (or everything at once from desktop/: npm run start is not wired; use desktop:start)
cd desktop && npm run desktop:start
```

Renderer unit tests (vitest, offline — Electron not needed):

```bash
cd desktop && npm run test:unit -- --run
cd desktop && npm run lint
```

Bot API smoke:

```bash
python3 scripts/smoke_api.py
```

### API server (`bot.py api`)

- Binds `127.0.0.1` only (refuses non-loopback hosts); default port **4769**, overridable
  via `--port` or the `NOTIFYBOT_API_PORT` env var.
- Endpoints: `GET /api/health`, `GET /api/events?limit=&since=&kind=`, `GET|POST
  /api/config`, `GET /api/finance/summary|transactions?month=YYYY-MM`,
  `GET|POST /api/finance/report?month=`, `POST /api/test-notify`, `POST
  /api/agent/turn`, `GET /api/agent/sessions`, `GET /api/agent/session/{id}`,
  `GET /api/services`, plus `GET /api/logs/tail?lines=N` (raw tail for the Logs
  screen). Unknown paths/methods answer `404/405` as JSON.
- The API **never spawns or manages the daemon** and never touches the Notification
  Center db2; finance reads use read-only SQLite connections (`mode=ro`, 3s busy
  timeout); config writes are atomic (tmp file + `os.replace`) with schema-lite
  validation; TG secrets are env-only (`TELEGRAM_BOT_TOKEN`/`TG_BOT_TOKEN`,
  `TELEGRAM_ALLOWED_USERS`/`TG_CHAT_ID`) and rejected if non-empty — a non-empty
  token/chat_id in config.json shows as `********` over the API.
- `POST /api/test-notify` from the Electron app may trigger a one-time
  Notifications/Automation consent attributed to the Electron host (macOS TCC) —
  accept once. The electron-spawned daemon is expected to have the same zero-TCC db2
  read behavior as Terminal-spawned Python, but that is verified manually (see below).

### Electron process management

- `desktop/electron/src/processManager.ts` spawns `<python> bot.py api --port <P>` and
  `<python> bot.py` (repo root = two levels above `desktop/electron`, override with
  `BOT_REPO_PATH`; Python binary override with `NOTIFYBOT_PYTHON`, default
  `/usr/bin/python3`) and SIGTERMs both on quit. If a healthy API already answers on
  the preferred port, the app **adopts** it instead of spawning — so you can run the
  bot in Terminal and let the UI ride along.
- The on-screen Electron smoke (window, tabs, live feed, artifact iframe) is manual —
  boot `cd desktop/electron && npm start` and click through the five tabs.
- Debug boot: `cd desktop/electron && npm run electron:start-debug` (port 9223).

## Agent & Chat (in-app tool pipeline)

The Chat tab talks to an in-process agent inside the existing `bot.py api` server
(`notifybot/agent.py` + `notifybot/agent_tools.py` — stdlib only, no new processes).
Each turn runs a strict **one-JSON-per-turn tool loop**: the model either calls one
tool (`{"tool", "args"}`) or answers (`{"answer": <PT-BR text>, "urls": [...]}`). The
final assistant reply is always in Brazilian Portuguese; everything else (prompts,
code, tool JSON) is English.

### Tools

| Tool | Args | Behavior |
| --- | --- | --- |
| `app.data` | `{"dataset", "params?"}` | Reads bot datasets through the same handlers as the JSON API: `health`, `events?since=&limit=&kind=`, `finance_summary?month=`, `finance_transactions?month=`. Errors surface as tool `{"error"}` results. |
| `dashboard.create` | `{"title", "html", "data_source"}` | Server-side `artifact_id` (uuid4 hex); validates the HTML is self-contained (rejects `<script>`, external refs, `@import`, `url(http`, `fetch(`, `XMLHttpRequest`, >512 KB) and writes `<config_dir>/artifacts/<id>.html` (rotation: newest 200 kept). |
| `server.serve` | `{"artifact_id"}` | Ensures the loopback artifacts sub-service is healthy on **127.0.0.1:4770** (fixed constant; port-0 injection is test-only via `NOTIFYBOT_ARTIFACTS_PORT`) and returns the artifact URL. |
| `notify.test` | `{}` | Posts 3 REAL macOS notifications (reuses `POST /api/test-notify` semantics); osascript failures surface as tool errors. |

Loop guards: max 6 tool turns, ~90 s global budget, ~4 KB tool-result truncation in
context, one protocol-reminder retry on unparsable model output, and graceful PT-BR
error answers (HTTP 200) when Ollama is unreachable — failures never 500 the turn.
Sessions persist append-only under `<config_dir>/data/agent_sessions/<id>.jsonl`;
ids match `^[A-Za-z0-9-]{1,64}$`.

### API rows

- `POST /api/agent/turn` `{session_id?, message}` (synchronous; missing/empty/oversize
  message → 400, unknown session → 404).
- `GET /api/agent/sessions` — newest-first summaries.
- `GET /api/agent/session/{id}` — full transcript (id regex-validated).
- `GET /api/services` — sub-service registry (`artifacts` running/port/root/since).

### Rendering + CSP

When the answer carries `urls[]`, ChatView renders
`<iframe sandbox src="http://127.0.0.1:4770/<id>.html">` — an EMPTY `sandbox` attribute
(no scripts) combined with the `<script>`-free artifact validation. The Electron CSP
adds exactly one directive: `frame-src 'self' <scheme>://* http://127.0.0.1:4770`
(fixed port — deliberate, so no other local service may be framed); the renderer
additionally allowlists only `http://127.0.0.1:4770/<32-hex>.html` URLs.

### Agent smoke (mock mode, no Ollama needed)

```bash
NOTIFYBOT_SMOKE_AGENT_MOCK=1 python3 scripts/smoke_api.py
```

`NOTIFYBOT_SMOKE_AGENT_MOCK=1` selects a deterministic scripted LLM (test/smoke-only
hook documented in `notifybot/agent.py`) that walks the dashboard pipeline
(`app.data` → `dashboard.create` → `server.serve` → answer with urls). Unit tests
inject their own `llm_fn` transcripts instead (`tests/test_agent.py`);

> If something else owns port 4770 when `server.serve` runs, the tool fails with a
> clear error ("artifacts port 4770 busy — close the other process"); there is no
> auto-increment by design.

## Project layout

```
bot.py                  Thin CLI entrypoint (forwards to notifybot.cli.main)
notifybot/              Implementation package:
  parser.py               binary plist decoding, CoreData timestamps, UUID helpers
  reader.py               Notification Center db2 snapshot reader
  state.py                watermark + recent-UUID dedup manager
  classifier.py           Ollama classification with tolerant JSON parsing
  router.py               Router v2: deterministic rules + LLM action router
  actions.py              action dispatcher (log/telegram/command/registry/handlers)
  finance.py              FinanceCurator (regex + LLM fallback) + FinanceLedger (SQLite)
  report.py               inline SVG chart engine + FinanceReporter (HTML/markdown/PT-BR insights)
  agent_tools.py          agent tools (app.data/dashboard.create/server.serve/notify.test)
                          + the 4770 artifacts sub-service registry
  agent.py                in-process agent tool loop + agent session JSONL store
  agent_prompt.md         English system prompt template for the agent
  cli.py                  CLI logic: poll loop wiring, `report` subcommand, config defaults
config.json             Runtime configuration (polling, Ollama, Telegram, router,
                        actions registry, finance, rules)
tests/                  Offline unit suite (router, finance, report, bot):
                          python3 -m unittest discover -s tests
scripts/
  smoke_e2e.py          Phase 1 end-to-end smoke test:
                          python3 scripts/smoke_e2e.py
  smoke_finance.py      Phase 2 finance end-to-end smoke test:
                          python3 scripts/smoke_finance.py
  smoke_api.py          Desktop-UI API smoke test (sandboxed):
                          python3 scripts/smoke_api.py
                        # agent rows (mock mode):
                        NOTIFYBOT_SMOKE_AGENT_MOCK=1 python3 scripts/smoke_api.py
desktop/                Desktop UI (Electron app), sibling of the Python app:
  package.json            renderer scaffold (Vue 3 + Vite + TS + Ionic + Pinia),
                          mirrored from hydranote's (no duckdb/tiptap/pdf/native deps)
  src/main.ts             Ionic boot + dark.system.css palette
  src/App.vue             IonTabs shell (Dashboard / Finance / Chat / Settings / Logs)
  src/router/index.ts     routes /dashboard /chat /finance /config /logs (/ -> /dashboard)
  src/types/bot.ts        TS payloads mirroring the bot's Python dicts
  src/notify.d.ts         `window.notifyAPI` bridge typings (preload-exposed)
  src/services/           notifyClient.ts (bridge / browser-dev fetch) + configValidation
  src/stores/bot.ts       Pinia store (status, events ring, finance cache, config draft)
  src/composables/        usePolling.ts (2s poll loop helper)
  src/viewModels/         eventViewModel.ts (event -> row mapping) + chatViewModel.ts
                          (pure chat state machine, 4770 artifact URL allowlist)
  src/views/              DashboardView / ChatView / FinanceView / ConfigView / LogsView
  tests/unit/             vitest specs (npm run test:unit -- --run)
  electron/               Electron wrapper (hydranote's scaffold, slimmed):
    src/index.ts            app boot, menu, IPC registration
    src/setup.ts            ElectronCapacitorApp (electron-serve scheme, CSP, window state)
    src/processManager.ts   spawns/adopts `bot.py api` + `bot.py`; SIGTERM on quit
    src/ipc/                botApiIpc (http proxy) / daemonIpc / reportIpc
    src/preload.ts          contextBridge 'notifyAPI' (api/daemon/report/app)
PLAN.md                 Implementation plan (steps, acceptance criteria, risk notes)
PLAN-PHASE2.md          Phase 2 plan (Router v2 + finance plugin)
RESEARCH.md             Architecture findings: approach evaluation, zero-TCC rationale,
                        Monterey vs Sequoia sections, AI routing notes
.bot_state.json         Runtime state (watermark + recent UUIDs) — gitignored
finance.db              SQLite finance ledger — gitignored; created by the bot
logs/
  events.jsonl          Runtime event log (JSONL) — gitignored; created by the bot
reports/                Generated monthly HTML reports — gitignored
```

The runtime artifacts (`.bot_state.json`, `logs/`, `finance.db` and `reports/`) are
generated in place and listed in `.gitignore`; do not commit them.