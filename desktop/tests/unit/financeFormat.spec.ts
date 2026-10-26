import { describe, expect, test } from 'vitest';

import {
  currentMonth,
  formatBrl,
  formatMonth,
  formatMomDelta,
  monthLabelPt,
  nextMonth,
  parseMonth,
  previousMonth
} from '../../src/utils/financeFormat';

describe('formatBrl', () => {
  test('formats plain values with pt-BR grouping', () => {
    expect(formatBrl(1234.56)).toBe('R$ 1.234,56');
    expect(formatBrl(50)).toBe('R$ 50,00');
    expect(formatBrl(0)).toBe('R$ 0,00');
    expect(formatBrl(1234567.89)).toBe('R$ 1.234.567,89');
  });

  test('negatives and string inputs', () => {
    expect(formatBrl(-12.3)).toBe('-R$ 12,30');
    expect(formatBrl('987.65')).toBe('R$ 987,65');
    expect(formatBrl('1.234,56')).toBe('R$ 1,23'); // not parseable as float -> clamped
  });

  test('garbage never throws (falls back to R$ 0,00)', () => {
    expect(formatBrl(null)).toBe('R$ 0,00');
    expect(formatBrl(undefined)).toBe('R$ 0,00');
    expect(formatBrl('banana')).toBe('R$ 0,00');
  });
});

describe('month arithmetic', () => {
  test('parseMonth accepts YYYY-MM and rejects others', () => {
    expect(parseMonth('2026-10')).toEqual([2026, 10]);
    expect(() => parseMonth('2026/10')).toThrow();
    expect(() => parseMonth('2026-13')).toThrow();
  });

  test('previous month crosses the January edge (2026-01 -> 2025-12)', () => {
    expect(previousMonth('2026-01')).toBe('2025-12');
    expect(previousMonth('2025-12')).toBe('2025-11');
    expect(previousMonth('2026-10')).toBe('2026-09');
  });

  test('next month crosses the December edge (2025-12 -> 2026-01)', () => {
    expect(nextMonth('2025-12')).toBe('2026-01');
    expect(nextMonth('2026-10')).toBe('2026-11');
    expect(nextMonth(nextMonth('2026-10'))).toBe('2026-12');
  });

  test('round trip: next(previous(x)) == x and previous(next(x)) == x', () => {
    for (const month of ['2026-01', '2025-12', '2026-10']) {
      expect(nextMonth(previousMonth(month))).toBe(month);
      expect(previousMonth(nextMonth(month))).toBe(month);
    }
  });

  test('formatMonth zero-pads and currentMonth is local', () => {
    expect(formatMonth(2026, 3)).toBe('2026-03');
    expect(typeof currentMonth()).toBe('string');
    expect(currentMonth()).toMatch(/^\d{4}-\d{2}$/);
  });
});

describe('labels', () => {
  test('monthLabelPt renders PT months', () => {
    expect(monthLabelPt('2026-10')).toBe('outubro de 2026');
    expect(monthLabelPt('2026-01')).toBe('janeiro de 2026');
    expect(monthLabelPt('2025-12')).toBe('dezembro de 2025');
  });

  test('formatMomDelta signs and formats', () => {
    expect(formatMomDelta(12.44)).toBe('+12,4%');
    expect(formatMomDelta(-3.14)).toBe('-3,1%');
    expect(formatMomDelta(0)).toBe('+0%');
    expect(formatMomDelta(null)).toBe(null);
    expect(formatMomDelta(undefined)).toBe(null);
  });
});