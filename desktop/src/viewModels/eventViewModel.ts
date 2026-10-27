/**
 * Event -> row presentation mapping for the Dashboard live feed
 * (tests: tests/unit/eventViewModel.spec.ts).
 */

import type { BotEvent, Classification } from '@/types/bot';

export type LayerValue = 'rule' | 'llm_router' | 'fallback_in_fallback' | '';

/** Match the /api/events `kind=` values accepted by notifybot/api.py. */
export type EventKindValue = 'all' | 'notification' | 'startup';

/** The 3-chip filter row (Status feed + Logs kind filter share the labels). */
export const EVENT_KIND_OPTIONS: ReadonlyArray<{ value: EventKindValue; label: string }> = [
  { value: 'all', label: 'All' },
  { value: 'notification', label: 'Events' },
  { value: 'startup', label: 'Startup' }
];

export interface EventRow {
  /** Stable dedupe key across polls (notifications by rec_id, startup by ts). */
  key: string;
  kind: 'notification' | 'startup';
  recId: number | null;
  timestamp: string;
  app: string;
  title: string;
  category: string;
  urgency: string;
  summary: string;
  layer: LayerValue;
  layerLabel: string;
  ruleMatched: string;
  actions: string[];
  confidencePct: string | null;
  telegramStatus: string | null;
  financeStatus: string | null;
  /** The untouched event dict — the expandable row shows it pretty-printed. */
  raw: BotEvent;
}

/** Expanded-row payload cap: keep the head, trim the tail (display only). */
const PAYLOAD_PREVIEW_MAX_CHARS = 2048;

export interface PayloadPreview {
  text: string;
  truncatedChars: number;
}

/** Pretty-printed event payload for the expanded row, trimmed at 2 KB. */
export function payloadPreview(event: BotEvent, maxChars = PAYLOAD_PREVIEW_MAX_CHARS): PayloadPreview {
  let text: string;
  try {
    text = JSON.stringify(event, null, 2);
  } catch (err) {
    text = String(event); // cyclic/throwing payload — degrade, never crash a render
  }
  if (text.length > maxChars) {
    return { text: text.slice(0, maxChars), truncatedChars: text.length - maxChars };
  }
  return { text, truncatedChars: 0 };
}

const LAYER_LABELS: Record<string, string> = {
  rule: 'Regra',
  llm_router: 'Router LLM',
  fallback_in_fallback: 'Fallback'
};

/** Deterministic key for dedup across polls. */
export function eventKey(event: BotEvent): string {
  if (event.event === 'startup') return `startup:${event.timestamp ?? ''}`;
  return `notification:${event.rec_id ?? event.timestamp ?? ''}`;
}

/** 0..1 fraction -> '87%'; anything else -> null. */
export function confidenceToPct(confidence: number | null | undefined): string | null {
  if (typeof confidence !== 'number' || !Number.isFinite(confidence)) return null;
  if (confidence < 0 || confidence > 1) return null;
  return `${Math.round(confidence * 100)}%`;
}

/** Classifier fallback semantics: other/low with title/body-derived summary. */
function classificationOf(event: BotEvent): { category: string; urgency: string; summary: string } {
  const cls = (event.classification ?? {}) as Partial<Classification> & Record<string, unknown>;
  const category = typeof cls.category === 'string' && cls.category !== '' ? cls.category : 'other';
  const urgency = typeof cls.urgency === 'string' && cls.urgency !== '' ? cls.urgency : 'low';
  let summary = typeof cls.summary === 'string' ? cls.summary : '';
  if (summary === '') {
    const fallback = String(event.title ?? '') || String(event.body ?? '');
    summary = fallback.length > 120 ? `${fallback.slice(0, 119)}…` : fallback;
  }
  return { category, urgency, summary };
}

export function eventToRow(event: BotEvent): EventRow {
  const kind: EventRow['kind'] = event.event === 'startup' ? 'startup' : 'notification';
  const layer = typeof event.layer === 'string' ? (event.layer as LayerValue) : '';
  const cls = classificationOf(event);
  const finance = event.finance;
  const results = event.action_results;
  return {
    key: eventKey(event),
    kind,
    recId: typeof event.rec_id === 'number' ? event.rec_id : null,
    timestamp: event.timestamp ?? '',
    app: String(event.app ?? '') || (kind === 'startup' ? 'bot' : '—'),
    title: String(event.title ?? ''),
    category: cls.category,
    urgency: cls.urgency,
    summary: kind === 'startup' ? `Bot iniciado no modo '${event.mode ?? 'daemon'}'` : cls.summary,
    layer: kind === 'startup' ? '' : layer,
    layerLabel: kind === 'startup' ? '' : LAYER_LABELS[layer] ?? layer,
    ruleMatched:
      typeof event.rule_matched === 'string' && event.rule_matched !== '' && event.rule_matched !== 'none'
        ? event.rule_matched
        : '',
    actions: Array.isArray(event.actions) ? event.actions.map(String) : [],
    confidencePct: kind === 'startup' ? null : confidenceToPct(event.confidence),
    telegramStatus: typeof event.telegram === 'string' ? event.telegram : (results?.telegram ?? null),
    financeStatus: finance && typeof finance.status === 'string' ? buildFinanceLabel(finance) : null,
    raw: event
  };
}

/** Visual identity kind for a feed row: the /api/events feed only knows
 * notification|startup, but rows that went through the finance layer get
 * their own cash-icon identity (tests: eventViewModel.spec.ts). */
export type EventKindIdentity = 'startup' | 'notification' | 'finance';

export function eventKindIdentity(row: EventRow): EventKindIdentity {
  if (row.kind === 'startup') return 'startup';
  return row.financeStatus !== null ? 'finance' : 'notification';
}

function buildFinanceLabel(finance: NonNullable<BotEvent['finance']>): string {
  const parts = [String(finance.status ?? '')];
  if (finance.method) parts.push(String(finance.method));
  if (finance.stored) parts.push(String(finance.stored));
  if (finance.reason) parts.push(String(finance.reason));
  return parts.filter((p) => p !== '').join(':');
}

/** Is this row a chip-worthy alert (telegram failed / finance error)? */
export function isErrorStatus(status: string | null): boolean {
  return typeof status === 'string' && status.startsWith('error');
}

/** Relative age label for a feed row: 'now' / '3 min ago' / '1 h ago' /
 * '2 d ago'. Pure in the render clock: the view-model passes `nowMs` so the
 * 2s poll just recomputes with a fresh tick. Empty for a missing or
 * unparseable timestamp; a future timestamp (clock skew) reads as 'now'. */
export function relativeTimeLabel(iso: string, nowMs: number): string {
  if (!iso || !Number.isFinite(nowMs)) return '';
  const time = new Date(iso).getTime();
  if (Number.isNaN(time)) return '';
  const diffMs = nowMs - time;
  if (diffMs < 60_000) return 'now';
  const minutes = Math.floor(diffMs / 60_000);
  if (minutes < 60) return `${minutes} min ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours} h ago`;
  return `${Math.floor(hours / 24)} d ago`;
}

/** R6: the SR feed-activity announcement text for feed growth — "N new
 * events" (singular at exactly 1). Empty for zero/negative deltas: a
 * kind-filter reload SHRINKS the ring (reset), and that reload announces
 * through aria-busy instead — never a fake "N new events". */
export function newEventsAnnouncement(previousCount: number, currentCount: number): string {
  const delta = currentCount - previousCount;
  if (!Number.isFinite(delta) || delta <= 0) return '';
  return delta === 1 ? '1 new event' : `${delta} new events`;
}

/** True when the filtered feed carries at least one row but ZERO
 * notification-kind rows (startup lines only) — the Status view then cues
 * the user that macOS itself hasn't produced anything yet (R2 finding 4).
 * An empty feed is NOT "all-startup": it renders the existing empty state
 * (tests: eventViewModel.spec.ts). */
export function feedIsAllStartup(rows: ReadonlyArray<EventRow>): boolean {
  return rows.length > 0 && !rows.some((row) => row.kind === 'notification');
}

/** Expanded-row micro caption: the source app plus the notification title —
 * the row label/bold title line now lives ONLY in the expanded view (R2
 * finding 1); collapsed rows show the summary (notifications) or the muted
 * kind tag next to the icon (startup). Startup rows carry no title, so
 * their caption stays just "bot" (tests: eventViewModel.spec.ts). */
export function expandedRowCaption(row: EventRow): string {
  const parts: string[] = [];
  if (row.app && row.app !== '—') parts.push(row.app);
  if (row.kind === 'notification' && row.title) parts.push(row.title);
  return parts.join(' · ');
}
