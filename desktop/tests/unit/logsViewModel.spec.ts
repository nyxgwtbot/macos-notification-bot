/**
 * Logs view model (loop2 iterB): client-side kind filtering of the raw
 * events.jsonl tail. Malformed lines carry no record kind, so they are only
 * ever visible under the All filter.
 */

import { describe, expect, test } from 'vitest';

import { filterLogLines, LOG_KIND_FILTER_OPTIONS, logLineKind } from '../../src/viewModels/logsViewModel';
import type { LogLineItem } from '../../src/stores/bot';

function line(lineNo: number, raw: string, malformed = false): LogLineItem {
  return { line_no: lineNo, raw, malformed };
}

const NOTIF = '{"event": "notification", "rec_id": 42, "title": "Fatura"}';
const STARTUP = '{"event": "startup", "timestamp": "2026-10-27T12:00:00Z", "mode": "daemon"}';
const OTHER = '{"event": "unknown_kind"}';
const MALFORMED = 'not-json at all';

describe('logLineKind', () => {
  test('classifies notification/startup records', () => {
    expect(logLineKind(NOTIF)).toBe('notification');
    expect(logLineKind(STARTUP)).toBe('startup');
  });

  test('unknown and malformed records yield null', () => {
    expect(logLineKind(OTHER)).toBe(null);
    expect(logLineKind('')).toBe(null);
    expect(logLineKind(MALFORMED)).toBe(null);
  });
});

describe('filterLogLines', () => {
  const lines = [line(1, NOTIF), line(2, STARTUP), line(3, MALFORMED, true), line(4, NOTIF)];

  test('All keeps every line including malformed ones', () => {
    expect(filterLogLines(lines, 'all')).toEqual(lines);
  });

  test('Events keeps only notification records (skips malformed)', () => {
    expect(filterLogLines(lines, 'notification').map((l) => l.line_no)).toEqual([1, 4]);
  });

  test('Startup keeps only startup records', () => {
    expect(filterLogLines(lines, 'startup').map((l) => l.line_no)).toEqual([2]);
  });
});

describe('LOG_KIND_FILTER_OPTIONS', () => {
  test('mirrors the Status feed kind chips', () => {
    expect(LOG_KIND_FILTER_OPTIONS.map((o) => o.label)).toEqual(['All', 'Events', 'Startup']);
  });
});