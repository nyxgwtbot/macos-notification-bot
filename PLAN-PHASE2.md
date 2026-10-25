## Objective
- Upgrade the macOS Notification Bot MVP into an LLM-driven action router ("Router v2") where actions are dynamically selected based on natural-language hints, and implement a production-grade, zero-dependency "finance" action plugin that extracts bank notification transactions, maintains a local SQLite ledger, and renders HTML reports with native inline SVG charts and PT-BR LLM insights.
- Acceptance criteria:
  1. `config.json` incorporates an extensible `"actions"` registry (`id`, `match_hint`, `type`, `enabled`, `config`) and a `"finance"` configuration section (`enabled`, `bank_apps`, `categories`, `ledger_db`, `telegram_summary`).
  2. Router v2 implements a two-stage routing pipeline: deterministic `config.rules` evaluate first (backward compatible); non-matching notifications pass to a secondary Ollama LLM call evaluated against the active actions registry; decisions below the confidence threshold (`default: 0.5`) or on error fall back to `"log"`.
  3. Every routing decision in `logs/events.jsonl` explicitly records the routing layer (`"rule"`, `"llm_router"`, or `"fallback"`), matched action, and confidence.
  4. The `"finance"` action implements dual-mode extraction: primary regex extraction for Brazilian currency formats (`R$ 1.234,56`, `R$ 50,00`, `123,45`) and transaction kinds (`gasto`, `recebimento`, `outro`), with tolerant LLM fallback extraction when regex fails.
  5. Transactions are persisted into `finance.db` (`expenses` table) idempotently by `rec_id` (`INSERT OR IGNORE`) with strict decimal-safe monetary arithmetic (`decimal.Decimal`).
  6. The CLI command `python3 bot.py report [--month YYYY-MM]` generates `reports/<month>.html` featuring zero-dependency inline SVG charts (category bar chart, category share donut chart, cumulative daily spend line) and 3–5 PT-BR LLM bullet insights, outputs a markdown summary to `stdout`, and optionally sends it via Telegram.
  7. Unit test suite passes offline (`python3 -m unittest discover -s tests`) with mocked LLM/Telegram calls, testing router parsing, regex extraction, ledger idempotency, SVG rendering, and report creation.
  8. `scripts/smoke_finance.py` demonstrates end-to-end functionality on macOS 12.7.6 by firing synthetic bank notifications via `osascript`, processing them via `bot.py --once`, verifying ledger rows, and asserting report generation with valid `<svg` elements and "Insights".
  9. `README.md` documents Router v2 architecture, action registration, finance tracking workflows, CLI usage, and sample configurations with PT-BR descriptions.

---

## Steps (ordered)

1. **Schema & Config Upgrade (`config.json`, `bot.py`)**
   - Files touched: `config.json`, `bot.py`.
   - Expected outcome: Define the Phase 2 configuration schema. Add top-level `"actions"` registry containing built-in definitions for `"log"`, `"telegram"`, and `"finance"`, plus command actions (`"command:<path>"`). Add `"router"` settings (`confidence_threshold: 0.5`, optional model override). Add `"finance"` configuration (`enabled: true`, `ledger_db: "finance.db"`, `telegram_summary: false`, default `bank_apps`, and standard categories: `alimentacao`, `transporte`, `compras`, `saude`, `lazer`, `servicos`, `assinaturas`, `outros`). Adjust `config.rules` to remove catch-all empty conditions so unhandled rules cleanly flow into Router v2.

2. **Router v2 Two-Stage Dispatcher (`bot.py` / `notification_bot/router.py`)**
   - Files touched: `bot.py` (or extracted module `notification_bot/router.py`).
   - Expected outcome: Implement `LLMRouter` and enhance `RuleRouter`. Pipeline logic: evaluate deterministic rules in `config.rules` first; if a specific rule matches, return action with layer `"rule"`. Otherwise, trigger a second Ollama `/api/chat` call with a structured prompt containing the notification content, classification, and the enabled actions registry (`id` and `match_hint`). Parse JSON output `{action_id, confidence, payload}` with markdown code-fence tolerance. If `action_id` is registered and `confidence >= confidence_threshold`, route with layer `"llm_router"`. If LLM fails, timed out, or low confidence, fall back deterministically to action `["log"]` with layer `"fallback"`.

3. **Event Logging & Action Registry Dispatcher Update (`bot.py`)**
   - Files touched: `bot.py`.
   - Expected outcome: Upgrade `ActionDispatcher.dispatch` to accept `routing_layer` (`"rule" | "llm_router" | "fallback"`), action confidence, and routing payload. Ensure event objects written to `logs/events.jsonl` include these fields alongside `rule_matched`. Register action executors for built-ins (`log`, `telegram`, `finance`) and type `"command"`, ensuring modular dispatching.

4. **Finance Transaction Curator & Regex Extraction (`finance.py` / `notification_bot/finance.py`)**
   - Files touched: `finance.py` (or `notification_bot/finance.py`).
   - Expected outcome: Implement `FinanceCurator`. Add regex-first extraction for Brazilian currency values (`R$ 1.234,56`, `R$ 1234,56`, `1.234,56`, `50,00`) across `title`, `subtitle`, and `body`. Classify transaction `kind` (`gasto`, `recebimento`, `outro`) using Brazilian banking keyword heuristics (e.g., "Pix enviado", "compra aprovada", "transferência recebida"). Implement an Ollama fallback extraction call returning `{amount, kind, category, description}` when regex matching fails. Map categories strictly to the configured vocabulary with `outros` as fallback. Ensure monetary amounts are parsed and converted safely using `decimal.Decimal`.

5. **Finance SQLite Ledger (`finance.py` / `notification_bot/finance.py`)**
   - Files touched: `finance.py` (or `notification_bot/finance.py`).
   - Expected outcome: Implement `FinanceLedger` wrapping `finance.db` using Python `sqlite3`. Ensure schema creation on initialization: `expenses(rec_id INTEGER PRIMARY KEY, ts TEXT, bundle TEXT, amount REAL, kind TEXT, category TEXT, description TEXT, raw_json TEXT)`. Provide atomic, idempotent record insertion via `INSERT OR IGNORE INTO expenses`. Implement ledger query helpers for monthly aggregates, daily totals, category breakdowns, and historical month comparisons using decimal arithmetic for all aggregations to avoid floating-point drift.

6. **Zero-Dependency Native SVG Chart Engine (`svg_charts.py` / `notification_bot/svg_charts.py`)**
   - Files touched: `svg_charts.py` (or `notification_bot/svg_charts.py`).
   - Expected outcome: Implement pure Python standard library SVG chart generators (no matplotlib, no pip packages):
     - `render_category_bar_chart(category_totals)`: Horizontal/vertical bar chart with axis labels and values.
     - `render_category_donut_chart(category_totals)`: SVG donut/pie chart displaying percentage share per category.
     - `render_daily_trend_chart(daily_totals, days_in_month)`: Polyline/path daily cumulative spend curve.
     Ensure all SVGs are standalone, scalable (`viewBox`), styled with accessible contrast, and safe against injection via `html.escape`.

7. **Finance Reporting Engine & PT-BR Insights (`reporting.py` / `notification_bot/reporting.py`)**
   - Files touched: `reporting.py` (or `notification_bot/reporting.py`).
   - Expected outcome: Implement `FinanceReporter`. Reads monthly ledger records, executes the SVG engine, and compiles `reports/<month>.html`. Queries Ollama with month summary metrics to generate 3–5 analytical bullet points in PT-BR (highlighting dominant expense category, single highest transaction, busiest day, and comparison to prior month when data exists), with a deterministic stdlib fallback if the LLM is unreachable. Formats a terminal-friendly PT-BR markdown summary for `stdout` and dispatches via Telegram when `config.finance.telegram_summary` is enabled.

8. **CLI Command & Loop Integration (`bot.py`)**
   - Files touched: `bot.py`.
   - Expected outcome: Wire the `"finance"` action into the poll loop (`run_cycle`) so incoming bank notifications automatically trigger curation and ledger ingestion. Add `report` CLI subcommand to `bot.py` (`python3 bot.py report [--month YYYY-MM]`) that parses arguments, queries `finance.db`, writes `reports/<month>.html`, prints the terminal summary, and handles optional Telegram forwarding.

9. **Unit Test Suite Expansion (`tests/test_router.py`, `tests/test_finance.py`, `tests/test_bot.py`)**
   - Files touched: `tests/test_router.py`, `tests/test_finance.py`, `tests/test_bot.py`.
   - Expected outcome: Expand offline test coverage (all external requests mocked, zero network):
     - Router v2: registry loading, deterministic rules precedence, LLM-router prompt generation, JSON response parsing, low-confidence fallback to `log`, and event logging structure.
     - Finance curation: regex amount extraction across multiple Brazilian currency formats, kind detection, category normalization, and decimal precision.
     - Finance ledger: table creation, `rec_id` deduplication/idempotency, and monthly aggregations.
     - SVG charts & Report: string assertions verifying valid `<svg>`, `<rect>`, `<path>`, and `<circle>` elements, HTML report generation on synthetic ledger data, and PT-BR fallback insights.
     Ensure `python3 -m unittest discover -s tests` runs and passes all tests.

10. **E2E Smoke Test & Documentation (`scripts/smoke_finance.py`, `README.md`)**
    - Files touched: `scripts/smoke_finance.py`, `README.md`.
    - Expected outcome:
      - Create `scripts/smoke_finance.py`: posts 3 synthetic bank notifications with distinct `R$` amounts via `osascript`, polls via `bot.py --once` (incorporating 2–6s db2 settle times and marker reuse to respect LLM call budget), verifies rows inserted in `finance.db`, runs report generation, asserts `reports/<month>.html` exists and contains `<svg` and "Insights", and prints `[PASSED]` with evidence.
      - Update `README.md`: add sections "Router v2 (LLM actions)" and "Finance tracking", describe configuration options, document the `report` CLI command, provide PT-BR prompt explanations, and update troubleshooting steps.

---

## Dependencies
- Step 2 depends on Step 1 (Router v2 relies on the upgraded `config.json` action registry and router settings).
- Step 3 depends on Step 2 (Event logging depends on router decision layer output).
- Step 5 depends on Step 4 (Ledger storage consumes curated transactions produced by the extraction engine).
- Step 6 has no code dependency on Step 5, but provides the chart components for Step 7.
- Step 7 depends on Step 5 and Step 6 (Reporting engine consumes ledger data and renders SVG charts).
- Step 8 depends on Steps 2, 3, 4, 5, and 7 (CLI entrypoint wires together the reader, router, finance action, and reporting command).
- Step 9 depends on Steps 2, 4, 5, 6, and 7 (Unit tests validate all router, finance, SVG, and reporting logic).
- Step 10 depends on Step 8 and Step 9 (E2E smoke test executes the integrated CLI and documentation covers the final system).

---

## Risks / Open questions
- **Catch-All Rule Precedence**: In Phase 1 `config.json`, a default rule with empty condition `{}` matched all notifications. If preserved, Router v2 would never be reached. *Mitigation*: Step 1 explicitly cleanses `config.rules` so only specific condition-based rules (e.g., `high_urgency`, `messages`) take precedence, allowing unmatched notifications to proceed to Router v2.
- **Two-Stage LLM Latency in Polling Loop**: Running classification followed immediately by router dispatch (and potentially finance fallback extraction) can introduce a ~5s latency per notification if Ollama is slow. *Mitigation*: Both LLM calls must enforce strict timeouts (10–15s max) with non-blocking fallbacks (`category: other` and `action: log`) so the poll cycle never hangs.
- **Brazilian Currency Regex Edge Cases**: Notifications from different banks format money differently (e.g., `R$ 1.250,00`, `R$12,50`, `R$ 45`, `compra de 30,00`). *Mitigation*: Use comprehensive regex patterns covering optional spaces, thousands separators, and cents, backed by Decimal casting and an LLM fallback extraction prompt for unusual sentence structures.
- **SVG Math & Zero-Data Boundary Conditions**: Generating SVG donut/pie and trend charts for months with zero expenses or a single transaction could produce division-by-zero or degenerate SVG paths. *Mitigation*: Implement guard clauses in `svg_charts.py` to render friendly empty-state SVG placeholders when transaction count or total spend is zero.
- **LLM Call Budget in Smoke Test**: Running multiple sequential LLM calls during automated smoke tests could exceed session budgets or cause flakiness. *Mitigation*: Follow established reflection patterns (`reflections/llm-budget-smoke-idempotency.md`) in `scripts/smoke_finance.py` with per-marker reuse and 2–6s db2 settle intervals before invoking `bot.py --once`.
