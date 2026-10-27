/**
 * Shared type definitions mirroring the Python payloads exposed by
 * `python3 bot.py api` (notifybot/api.py) and the events written by
 * notifybot/actions.py ActionDispatcher.dispatch — field-for-field.
 */

/** LLM classifier output (notifybot/classifier.py). */
export interface Classification {
  category: string;
  urgency: 'low' | 'medium' | 'high' | string;
  summary: string;
  [key: string]: unknown;
}

/** event.finance enrichment written by the finance action handler (cli.py). */
export interface FinanceEventStatus {
  status: string;
  reason?: string | null;
  method?: string | null;
  stored?: string | null;
  rec_id?: number | null;
  amount?: string | null;
  kind?: string | null;
  category?: string | null;
  description?: string | null;
}

/** One raw JSONL dict from logs/events.jsonl (actions.py dispatch / log_startup). */
export interface BotEvent {
  event: 'notification' | 'startup' | string;
  timestamp: string;
  rec_id?: number | null;
  app?: string | null;
  uuid?: string | null;
  title?: string | null;
  subtitle?: string | null;
  body?: string | null;
  classification?: Classification;
  /** Router v2 layer: "rule" | "llm_router" | "fallback_in_fallback" (router.py). */
  layer?: string | null;
  rule_matched?: string | null;
  confidence?: number | null;
  router_payload?: Record<string, unknown>;
  actions?: string[];
  action_results?: Record<string, string>;
  telegram?: string | null;
  finance?: FinanceEventStatus;
  // startup events carry these instead of the notification fields:
  mode?: 'daemon' | 'once' | string;
  watermark?: number | null;
}

/** GET /api/health response. */
export interface HealthResponse {
  ok: boolean;
  bot_version: string;
  api_version: string;
  watermark: { last_rec_id: number; updated_at: string | null };
  log: { path: string; bytes: number; mtime: number | null };
  finance: { enabled: boolean; db_exists: boolean };
  ollama: { url: string; model: string };
}

/** GET /api/events response. */
export interface EventsResponse {
  events: BotEvent[];
  latest_rec_id: number | null;
  count: number;
}

/** GET /api/config response / POST /api/config body (masked server-side). */
export interface BotConfig {
  poll_interval: number;
  state_file: string;
  log_file: string;
  ollama: { url: string; model: string; timeout: number };
  /** token/chat_id are env-driven; the API only ever returns "******** " masks or "". */
  telegram: { token: string; chat_id: string };
  router: { min_confidence: number; timeout: number };
  actions: ActionEntry[];
  rules: BotRule[];
  finance: {
    enabled: boolean;
    ledger_db: string;
    bank_apps: string[];
    telegram_summary: boolean;
    categories: string[];
  };
  [key: string]: unknown;
}

/** Entry of config.actions (cli.py defaults / actions.py registry). */
export interface ActionEntry {
  id: string;
  match_hint: string;
  type: 'builtin' | 'command';
  enabled: boolean;
  config: { command?: string; [key: string]: unknown };
}

/** Entry of config.rules (router.py RuleRouter). */
export interface BotRule {
  name: string;
  condition: Record<string, string | string[]>;
  actions: string[];
}

/** GET /api/finance/summary response (amounts as plain floats). */
export interface FinanceSummary {
  month: string;
  db_exists: boolean;
  count: number;
  total: number;
  by_category: Record<string, number>;
  by_kind: Record<string, number>;
  prev_total: number;
  mom_delta: number | null;
}

/** One row of GET /api/finance/transactions (finance.py month_records, minus raw_json). */
export interface FinanceTransaction {
  rec_id: number;
  ts: string;
  bundle: string;
  amount: number;
  kind: string;
  category: string;
  description: string;
}

export interface FinanceTransactionsResponse {
  month: string;
  records: FinanceTransaction[];
}

/** GET /api/finance/report response (status only — never generates). */
export interface FinanceReportStatus {
  month: string;
  html_path: string;
  exists: boolean;
}

/** POST /api/finance/report response. */
export interface FinanceReportResult {
  ok: boolean;
  month: string;
  html_path: string;
  markdown: string;
}

/** POST /api/test-notify response. */
export interface TestNotifyResponse {
  ok: boolean;
  posted: number;
}

/** One in-loop tool step of POST /api/agent/turn ("assistant" rows that
 * touched a tool; the python loop omits the failing key on each outcome). */
export interface AgentTurnStep {
  role: 'assistant' | string;
  tool: string;
  args?: Record<string, unknown>;
  /** Tool payload on success (arbitrary dataset/artifact JSON). */
  result?: unknown;
  /** Error text on tool failure (key omitted when the tool succeeded). */
  error?: string;
  ts: string;
}

/** POST /api/agent/turn response (synchronous turn). */
export interface AgentTurnResponse {
  session_id: string;
  turn: AgentTurnStep[];
  answer: string;
  urls: string[];
}

/** GET /api/agent/sessions row (newest-first list). */
export interface AgentSessionSummary {
  session_id: string;
  updated_at: string;
  /** Number of user messages in the session. */
  turns: number;
  /** First user message, truncated by the server. */
  preview: string;
}

export interface AgentSessionsResponse {
  sessions: AgentSessionSummary[];
}

/** One persisted JSONL record of GET /api/agent/session/<id>: a user message,
 * an assistant tool step (tool/args/result|error), or an assistant answer
 * (answer/urls). */
export interface AgentRecord {
  role: 'user' | 'assistant' | string;
  content?: string;
  tool?: string;
  args?: Record<string, unknown>;
  result?: unknown;
  error?: string;
  answer?: string;
  urls?: string[];
  ts: string;
}

/** GET /api/agent/session/<id> response. */
export interface AgentSessionDetail {
  session_id: string;
  records: AgentRecord[];
}

/** Row of GET /api/services -> services.artifacts (always present). */
export interface ServiceStatusRow {
  running: boolean;
  host: string;
  /** Bound port while running; the configured port otherwise. */
  port: number;
  root: string;
  since: string | null;
}

/** GET /api/services response. */
export interface ServicesResponse {
  services: { artifacts: ServiceStatusRow };
}

/** 400 {error, details:[…]} error bodies from the API. */
export interface ApiErrorBody {
  error: string;
  details?: string[];
}