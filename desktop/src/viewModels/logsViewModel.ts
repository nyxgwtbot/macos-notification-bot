/**
 * Logs view model (loop2 iterB): client-side record-kind filter for the raw
 * events.jsonl tail + line payload parsing. The kind values/labels mirror
 * EVENT_KIND_OPTIONS (Status feed chips) so both screens feel like the same
 * control. Pure functions only (tests: tests/unit/logsViewModel.spec.ts).
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

export interface LogLineSegment {
  text: string;
  cls: LogTokenClass | null;
}

const LOG_TOKEN_RE =
  /("(?:\\.|[^"\\])*")(?=\s*:)|("(?:\\.|[^"\\])*")|\b(true|false)\b|(-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)/g;

export function logLineSegments(raw: string): LogLineSegment[] {
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
  return segments;
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