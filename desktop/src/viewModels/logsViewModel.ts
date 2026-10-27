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