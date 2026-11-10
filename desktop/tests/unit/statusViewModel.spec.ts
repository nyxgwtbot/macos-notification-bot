import { describe, expect, test } from 'vitest';

import {
  DEFAULT_RUNTIME_API_PORT,
  ageLabel,
  formatApiPort
} from '../../src/viewModels/statusViewModel';

describe('formatApiPort', () => {
  test('exports DEFAULT_RUNTIME_API_PORT as 4769', () => {
    expect(DEFAULT_RUNTIME_API_PORT).toBe(4769);
  });

  test('returns "—" when daemonPort is null/undefined and no health/adopted/apiStarted flags are set', () => {
    expect(formatApiPort({})).toBe('—');
    expect(formatApiPort({ daemonPort: null })).toBe('—');
    expect(formatApiPort({ daemonPort: undefined })).toBe('—');
    expect(formatApiPort({ daemonPort: null, adopted: false, hasHealth: false, apiStarted: false })).toBe('—');
  });

  test('returns "<port>" when explicit daemonPort is provided and not adopted', () => {
    expect(formatApiPort({ daemonPort: 4769 })).toBe('4769');
    expect(formatApiPort({ daemonPort: 8080, adopted: false })).toBe('8080');
    expect(formatApiPort({ daemonPort: 9000, hasHealth: true })).toBe('9000');
  });

  test('returns "<port> (adopted)" when explicit daemonPort is provided and adopted is true', () => {
    expect(formatApiPort({ daemonPort: 8080, adopted: true })).toBe('8080 (adopted)');
    expect(formatApiPort({ daemonPort: 4769, adopted: true })).toBe('4769 (adopted)');
  });

  test('returns "4769 (adopted)" when daemonPort is absent but adopted is true', () => {
    expect(formatApiPort({ adopted: true })).toBe('4769 (adopted)');
    expect(formatApiPort({ daemonPort: null, adopted: true })).toBe('4769 (adopted)');
  });

  test('returns "4769" when daemonPort is absent but hasHealth is true', () => {
    expect(formatApiPort({ hasHealth: true })).toBe('4769');
    expect(formatApiPort({ daemonPort: null, hasHealth: true })).toBe('4769');
  });

  test('returns "4769" when daemonPort is absent but apiStarted is true', () => {
    expect(formatApiPort({ apiStarted: true })).toBe('4769');
    expect(formatApiPort({ daemonPort: null, apiStarted: true })).toBe('4769');
  });
});

describe('ageLabel', () => {
  // Fixed render clock; timestamps are built as offsets from it.
  const NOW = Date.parse('2026-10-27T12:00:00.000Z');

  test('uses seconds granularity for young stamps ("4s ago" / "12s ago")', () => {
    expect(ageLabel(new Date(NOW).toISOString(), NOW)).toBe('0s ago');
    expect(ageLabel(new Date(NOW - 4_000).toISOString(), NOW)).toBe('4s ago');
    expect(ageLabel(new Date(NOW - 12_000).toISOString(), NOW)).toBe('12s ago');
    expect(ageLabel(new Date(NOW - 59_000).toISOString(), NOW)).toBe('59s ago');
  });

  test('steps up through min/h/d at the exact boundaries', () => {
    expect(ageLabel(new Date(NOW - 60_000).toISOString(), NOW)).toBe('1 min ago');
    expect(ageLabel(new Date(NOW - 12 * 60_000).toISOString(), NOW)).toBe('12 min ago');
    expect(ageLabel(new Date(NOW - 59 * 60_000).toISOString(), NOW)).toBe('59 min ago');
    expect(ageLabel(new Date(NOW - 60 * 60_000).toISOString(), NOW)).toBe('1 h ago');
    expect(ageLabel(new Date(NOW - 23 * 60 * 60_000).toISOString(), NOW)).toBe('23 h ago');
    expect(ageLabel(new Date(NOW - 24 * 60 * 60_000).toISOString(), NOW)).toBe('1 d ago');
    expect(ageLabel(new Date(NOW - 2.5 * 24 * 60 * 60_000).toISOString(), NOW)).toBe('2 d ago');
  });

  test('clock skew (future stamp) clamps to "0s ago", never a negative age', () => {
    expect(ageLabel(new Date(NOW + 5_000).toISOString(), NOW)).toBe('0s ago');
    expect(ageLabel(new Date(NOW + 10 * 60_000).toISOString(), NOW)).toBe('0s ago');
  });

  test('empty, malformed or non-finite inputs degrade to empty string', () => {
    expect(ageLabel('', NOW)).toBe('');
    expect(ageLabel(null, NOW)).toBe('');
    expect(ageLabel(undefined, NOW)).toBe('');
    expect(ageLabel('not-a-date', NOW)).toBe('');
    expect(ageLabel(new Date(NOW).toISOString(), Number.NaN)).toBe('');
  });
});
