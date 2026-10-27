import { describe, expect, test } from 'vitest';

import {
  DEFAULT_RUNTIME_API_PORT,
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
