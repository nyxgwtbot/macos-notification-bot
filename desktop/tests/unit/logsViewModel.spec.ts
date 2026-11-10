/**
 * Logs view model (loop2 iterB): client-side kind filtering of the raw
 * events.jsonl tail. Malformed lines carry no record kind, so they are only
 * ever visible under the All filter.
 */

import { describe, expect, test } from 'vitest';

import {
  LOGS_DENSITY_KEY,
  LOGS_FOLLOW_KEY,
  LOGS_KIND_FILTER_KEY,
  filterLogLines,
  filterLogLinesByQuery,
  logLineKind,
  logLineSegments,
  logNewLinesAnnouncement,
  parseLogDensity,
  parseLogFollow,
  parseLogKindFilter,
  LOG_KIND_FILTER_OPTIONS
} from '../../src/viewModels/logsViewModel';
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

describe('filterLogLinesByQuery (R3 search)', () => {
  const lines = [
    line(1, NOTIF),
    line(2, STARTUP),
    line(3, MALFORMED, true),
    line(4, '{"event": "notification", "body": "Pagamento confirmado"}')
  ];

  test('empty and whitespace-only queries pass every line through', () => {
    expect(filterLogLinesByQuery(lines, '')).toEqual(lines);
    expect(filterLogLinesByQuery(lines, '   ')).toEqual(lines);
  });

  test('case-insensitive substring over the raw line', () => {
    expect(filterLogLinesByQuery(lines, 'fatura').map((l) => l.line_no)).toEqual([1]);
    expect(filterLogLinesByQuery(lines, 'EVENT').map((l) => l.line_no)).toEqual([1, 2, 4]);
  });

  test('matches malformed raw text too (client-side substring)', () => {
    expect(filterLogLinesByQuery(lines, 'not-json').map((l) => l.line_no)).toEqual([3]);
  });

  test('no match yields an empty list', () => {
    expect(filterLogLinesByQuery(lines, 'zzz-nothing')).toEqual([]);
  });
});

describe('logLineSegments (R3 token dimming)', () => {
  test('JSON keys carry the muted nb-tok-key class', () => {
    const segs = logLineSegments('{"event": "startup"}');
    expect(segs.filter((s) => s.cls === 'nb-tok-key').map((s) => s.text)).toEqual(['"event"']);
  });

  test('numeric and boolean values carry the teal nb-tok-num class', () => {
    const segs = logLineSegments('{"rec_id": 42, "telegram": true, "ok": false}');
    const nums = segs.filter((s) => s.cls === 'nb-tok-num').map((s) => s.text);
    expect(nums).toEqual(['42', 'true', 'false']);
  });

  test('string values stay plain — digits and words inside them never recolor', () => {
    const raw = '{"title": "Invoice 42 and false claims"}';
    const segs = logLineSegments(raw);
    expect(segs.filter((s) => s.cls === 'nb-tok-num')).toEqual([]);
    expect(segs.filter((s) => s.cls === 'nb-tok-key').map((s) => s.text)).toEqual(['"title"']);
  });

  test('segments reconstruct the raw line exactly — single pass, nothing dropped', () => {
    const raw = '{"a":1,"b":true,"c":null,"arr":[1,2.5,-3e2],"s":"x","esc":"quote \\" here"}';
    const segs = logLineSegments(raw);
    expect(segs.map((s) => s.text).join('')).toBe(raw);
    const classes = segs.filter((s) => s.cls).map((s) => ({ t: s.text, c: s.cls }));
    expect(classes).toContainEqual({ t: '"a"', c: 'nb-tok-key' });
    expect(classes).toContainEqual({ t: '1', c: 'nb-tok-num' });
    expect(classes).toContainEqual({ t: 'true', c: 'nb-tok-num' });
    expect(classes).toContainEqual({ t: '-3e2', c: 'nb-tok-num' });
    // null stays unstyled (only numeric/bool values dim)
    expect(classes.some(({ t }) => t === 'null')).toBe(false);
  });

  /** Escape-then-wrap contract (VM half): hostile text stays PLAIN text with
   * no token class — the view's interpolated spans will HTML-escape it, so a
   * line containing `<script>` can never become markup. The rendered-DOM
   * half of this proof lives in tests/unit/LogsViewRender.spec.ts. */
  test('a <script> payload is plain unstyled text — never a token, never reconstructed as markup', () => {
    const raw = '{"title": "<script>alert(1)</script>", "ok": true}';
    const segs = logLineSegments(raw);
    expect(segs.map((s) => s.text).join('')).toBe(raw);
    const scriptSegs = segs.filter((s) => s.text.includes('<script>'));
    expect(scriptSegs.length).toBeGreaterThan(0);
    expect(scriptSegs.every((s) => s.cls === null)).toBe(true);
  });

  test('empty raw yields empty segments', () => {
    expect(logLineSegments('')).toEqual([]);
  });
});

/* ========================================
   R7: SEARCH-MATCH HIGHLIGHT — the live query wraps its matches in an
   nb-tok-hi span; escape-then-wrap stays intact (segments carry raw text;
   the rendered-DOM half lives in LogsViewRender.spec.ts).
   ======================================== */
describe('logLineSegments query highlighting (R7)', () => {
  test('matched pieces carry nb-tok-hi; surrounding pieces keep their token classes', () => {
    const segs = logLineSegments('{"event": "startup", "mode": "daemon"}', 'mode');
    // the match lands INSIDE the key — the surrounding quote marks keep the
    // muted key class while the matched substring re-classes to the highlight
    expect(segs.filter((s) => s.cls === 'nb-tok-hi').map((s) => s.text)).toEqual(['mode']);
    expect(segs.filter((s) => s.cls === 'nb-tok-key').map((s) => s.text)).toContain('"event"');
  });

  test('matches are case-insensitive and multi-occurrence', () => {
    const segs = logLineSegments('{"telegram": false, "state": "TELEGRAM_SENT"}', 'telegram');
    const his = segs.filter((s) => s.cls === 'nb-tok-hi');
    expect(his.map((s) => s.text)).toEqual(['telegram', 'TELEGRAM']);
  });

  test('matches inside a string value highlight too (substring over the raw line)', () => {
    const segs = logLineSegments('{"title": "Pagamento confirmado", "rec_id": 7}', 'confirma');
    expect(segs.filter((s) => s.cls === 'nb-tok-hi').map((s) => s.text)).toEqual(['confirma']);
  });

  test('segments still reconstruct the raw line exactly with a query applied', () => {
    const raw = '{"a":1,"b":true,"s":"xx bb xx"}';
    const segs = logLineSegments(raw, 'b');
    expect(segs.map((s) => s.text).join('')).toBe(raw);
  });

  test('a whitespace-only query passes segments through unchanged', () => {
    const plain = logLineSegments('{"a":1}');
    const withQuery = logLineSegments('{"a":1}', '   ');
    expect(withQuery).toEqual(plain);
  });

  test('empty query matches the no-query result', () => {
    expect(logLineSegments('{"a":1}', '')).toEqual(logLineSegments('{"a":1}'));
  });

  test('a <script> query match is still raw-text segments — the highlight span wraps escaped text only', () => {
    const raw = '{"title": "<script>alert(1)</script>"}';
    const segs = logLineSegments(raw, '"<script');
    const his = segs.filter((s) => s.cls === 'nb-tok-hi');
    expect(his.map((s) => s.text)).toEqual(['"<script']);
    // reconstruction keeps every character — no reassembly into markup
    expect(segs.map((s) => s.text).join('')).toBe(raw);
  });
});

/* ========================================
   R7: PERSISTENCE KEYS — follow-tail and kind filter survive reloads under
   the LOGS_DENSITY_KEY contract: tampered stored values degrade to defaults
   (follow ON, kind ALL).
   ======================================== */
describe('R7 persistence keys and parsers', () => {
  test('keys sit in the notifybot.logs.* namespace', () => {
    expect(LOGS_FOLLOW_KEY).toBe('notifybot.logs.follow');
    expect(LOGS_KIND_FILTER_KEY).toBe('notifybot.logs.kindFilter');
    expect(LOGS_DENSITY_KEY).toBe('notifybot.logs.density');
  });

  test('parseLogFollow reads stored \'false\', degrades everything else to ON', () => {
    expect(parseLogFollow('false')).toBe(false);
    expect(parseLogFollow('true')).toBe(true);
    expect(parseLogFollow(null)).toBe(true);
    expect(parseLogFollow('mega')).toBe(true);
    expect(parseLogFollow('')).toBe(true);
  });

  test('parseLogKindFilter whitelists the chip values, degrades tampered to all', () => {
    for (const option of LOG_KIND_FILTER_OPTIONS) {
      expect(parseLogKindFilter(option.value)).toBe(option.value);
    }
    expect(parseLogKindFilter(null)).toBe('all');
    expect(parseLogKindFilter('')).toBe('all');
    expect(parseLogKindFilter('startup!!')).toBe('all');
  });
});

describe('logNewLinesAnnouncement (R7 badge SR announce)', () => {
  test('the announce strings for the +N badge', () => {
    expect(logNewLinesAnnouncement(1)).toBe('1 new log line');
    expect(logNewLinesAnnouncement(42)).toBe('42 new log lines');
  });

  test('zero and negative counts announce nothing', () => {
    expect(logNewLinesAnnouncement(0)).toBe('');
    expect(logNewLinesAnnouncement(-3)).toBe('');
    expect(logNewLinesAnnouncement(Number.NaN)).toBe('');
  });
});

describe('parseLogDensity (R3 density toggle)', () => {
  test('compact parses to compact', () => {
    expect(parseLogDensity('compact')).toBe('compact');
  });

  test('comfortable, unknown, and null degrade to the comfortable default', () => {
    expect(parseLogDensity('comfortable')).toBe('comfortable');
    expect(parseLogDensity('mega')).toBe('comfortable');
    expect(parseLogDensity(null)).toBe('comfortable');
  });

  test('localStorage key constant used by the view', () => {
    expect(LOGS_DENSITY_KEY).toBe('notifybot.logs.density');
  });
});