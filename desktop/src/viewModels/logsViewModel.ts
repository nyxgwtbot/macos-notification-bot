/**
 * Logs view model (loop2 iterB): client-side record-kind filter for the raw
 * events.jsonl tail + line payload parsing. The kind values/labels mirror
 * EVENT_KIND_OPTIONS (Status feed chips) so both screens feel like the same
 * control. Pure functions only (tests: tests/unit/logsViewModel.spec.ts).
 *
 * R7: search-match highlighting (nb-tok-hi segments), follow/kind-filter
 * persistence keys + whitelist parsers, and the +N badge SR announcement.
 */

import type { LogLineItem } from '@/stores/bot';
import type { EventKindValue } from '@/viewModels/eventViewModel';

export type LogKindFilter = EventKindValue;
export const LOG_KIND_FILTER_OPTIONS: ReadonlyArray<{ value: LogKindFilter; label: string }> = [
  { value: 'all', label: 'All' },
  { value: 'notification', label: 'Events' },
  { value: 'startup', label: 'Startup' }
];

/** One JSONL line's record kind; unparseable/malformed lines yield null. */
export function logLineKind(raw: string): 'notification' | 'startup' | null {
  try {
    const parsed = JSON.parse(raw) as { event?: unknown };
    if (parsed && parsed.event === 'startup') return 'startup';
    if (parsed && parsed.event === 'notification') return 'notification';
    return null;
  } catch {
    return null;
  }
}

/**
 * Kind filter for the raw tail; malformed lines are only visible in All
 * (they carry no parseable record kind) so dimmed debug rows are never
 * silently swallowed.
 */
export function filterLogLines(lines: LogLineItem[], kind: LogKindFilter): LogLineItem[] {
  if (kind === 'all') return lines;
  return lines.filter((line) => logLineKind(line.raw) === kind);
}

/* ========================================
   R3: LOG SEARCH — substring filter over the loaded tail (runs client-side
   AFTER the kind filter), so Follow/pause behavior stays independent of the
   query (the view owns the 150ms debounce).
   ======================================== */

/** Case-insensitive substring filter; an empty/whitespace-only query passes
 * every line through. Matches the raw line text, malformed rows included. */
export function filterLogLinesByQuery(lines: LogLineItem[], query: string): LogLineItem[] {
  const needle = query.trim().toLowerCase();
  if (needle === '') return lines;
  return lines.filter((line) => line.raw.toLowerCase().includes(needle));
}

/* ========================================
   R3: TOKEN DIMMING — ONE regex pass per line. A quoted token followed by
   `:` is a key (muted span); bare true/false/number scalars are values
   (teal span). String values are consumed wholesale as plain segments, so
   digits/words inside them never recolor.

   SAFETY CONTRACT (proven in tests/unit/LogsViewRender.spec.ts): segments
   carry RAW text and render through Vue interpolations, which HTML-escape
   every text node before insertion, then spans wrap them — escape-then-wrap,
   single escape path, and NO innerHTML anywhere: a line containing
   `<script>` can never become markup.
   ======================================== */

export type LogTokenClass = 'nb-tok-key' | 'nb-tok-num';

/** R7: the live search-query match class slots into the same segment model —
 * matched pieces override the token class for their span. */
export type LogSegmentClass = LogTokenClass | 'nb-tok-hi';

export interface LogLineSegment {
  text: string;
  cls: LogSegmentClass | null;
}

const LOG_TOKEN_RE =
  /("(?:\\.|[^"\\])*")(?=\s*:)|("(?:\\.|[^"\\])*")|\b(true|false)\b|(-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)/g;

/** R7 query-highlight splitter: one token segment becomes zero or more
 * pieces; case-insensitive occurrences of the needle are re-classed to
 * 'nb-tok-hi', untouched pieces keep the token's class. Segments are still
 * RAW text — the escape-then-wrap contract (view interpolations escape every
 * text node before spans wrap them) is untouched by the highlight pass. */
function splitSegmentsByQuery(needle: string, text: string, cls: LogSegmentClass | null): LogLineSegment[] {
  if (needle === '') return [{ text, cls }];
  const lower = text.toLowerCase();
  const needleLower = needle.toLowerCase();
  if (needleLower === '') return [{ text, cls }];
  const parts: LogLineSegment[] = [];
  let from = 0;
  for (
    let hit = lower.indexOf(needleLower);
    hit !== -1;
    from = hit + needleLower.length, hit = lower.indexOf(needleLower, from)
  ) {
    if (hit > from) parts.push({ text: text.slice(from, hit), cls });
    parts.push({ text: text.slice(hit, hit + needleLower.length), cls: 'nb-tok-hi' });
  }
  if (from < text.length) parts.push({ text: text.slice(from), cls });
  return parts;
}

export function logLineSegments(raw: string, query = ''): LogLineSegment[] {
  const segments: LogLineSegment[] = [];
  let last = 0;
  LOG_TOKEN_RE.lastIndex = 0; // global-flag regex: reset before each pass
  for (let m = LOG_TOKEN_RE.exec(raw); m !== null; m = LOG_TOKEN_RE.exec(raw)) {
    if (m.index > last) segments.push({ text: raw.slice(last, m.index), cls: null });
    segments.push({
      text: m[0],
      cls: m[1] ? 'nb-tok-key' : m[3] || m[4] ? 'nb-tok-num' : null
    });
    last = m.index + m[0].length;
  }
  if (last < raw.length) segments.push({ text: raw.slice(last), cls: null });
  const needle = query.trim();
  if (needle === '') return segments;
  return segments.flatMap((segment) => splitSegmentsByQuery(needle, segment.text, segment.cls));
}

/* ========================================
   R3: LINE DENSITY — compact (fs 11 / lh 1.35) vs comfortable (fs 12 /
   lh 1.65), persisted under LOGS_DENSITY_KEY. Only the whitelist parse
   lives here so a tampered stored value can't inject anything.
   ======================================== */

export const LOGS_DENSITY_KEY = 'notifybot.logs.density';

export type LogDensity = 'compact' | 'comfortable';

/** Unknown/null stored values fall back to the comfortable default. */
export function parseLogDensity(raw: string | null): LogDensity {
  return raw === 'compact' ? 'compact' : 'comfortable';
}

/* ========================================
   R7 PERSISTENCE — follow-tail and kind filter survive reloads under the
   same whitelist-parse contract as LOGS_DENSITY_KEY: tampered stored values
   degrade to the defaults (follow ON, kind ALL). Only the parse lives here
   so a tampered value can't inject anything.
   ======================================== */

export const LOGS_FOLLOW_KEY = 'notifybot.logs.follow';

export const LOGS_KIND_FILTER_KEY = 'notifybot.logs.kindFilter';

/** 'false' reads as paused; null/tampered/anything else degrades to ON. */
export function parseLogFollow(raw: string | null): boolean {
  return raw !== 'false';
}

/** Null/tampered stored kinds degrade to 'all' (whitelist against the chips). */
export function parseLogKindFilter(raw: string | null): LogKindFilter {
  const option = LOG_KIND_FILTER_OPTIONS.find((entry) => entry.value === raw);
  return option ? option.value : 'all';
}

/** R7: the +N badge SR announcement — "N new log lines" (singular at
 * exactly 1). Empty for zero/negative counts: the badge renders only for a
 * positive count, and the SR region must not carry a stale or fake text. */
export function logNewLinesAnnouncement(count: number): string {
  if (!Number.isFinite(count) || count <= 0) return '';
  return count === 1 ? '1 new log line' : `${count} new log lines`;
}