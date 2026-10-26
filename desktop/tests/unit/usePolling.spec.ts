import { afterEach, beforeEach, describe, expect, test, vi } from 'vitest';

import { usePolling } from '../../src/composables/usePolling';

describe('usePolling', () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  test('runs immediately on start and then every interval', async () => {
    const task = vi.fn().mockResolvedValue(undefined);
    const poller = usePolling(task, { intervalMs: 2000 });

    poller.start();
    expect(poller.isActive.value).toBe(true);

    await vi.advanceTimersByTimeAsync(0);
    expect(task).toHaveBeenCalledTimes(1); // immediate first run

    await vi.advanceTimersByTimeAsync(2000);
    expect(task).toHaveBeenCalledTimes(2);

    await vi.advanceTimersByTimeAsync(4000);
    expect(task).toHaveBeenCalledTimes(4);

    poller.stop();
  });

  test('stop cancels further runs and restarting is not double-timed', async () => {
    const task = vi.fn().mockResolvedValue(undefined);
    const poller = usePolling(task, { intervalMs: 1000, immediate: false });

    poller.start();
    poller.start(); // no double timer
    poller.stop();
    await vi.advanceTimersByTimeAsync(5000);
    expect(task).not.toHaveBeenCalled();

    poller.start(); // restart works (immediate disabled here)
    expect(poller.isActive.value).toBe(true);
    await vi.advanceTimersByTimeAsync(1000);
    expect(task).toHaveBeenCalledTimes(1);
    poller.stop();
  });

  test('immediate:false waits for the first interval', async () => {
    const task = vi.fn().mockResolvedValue(undefined);
    const poller = usePolling(task, { intervalMs: 1000, immediate: false });

    poller.start();
    await vi.advanceTimersByTimeAsync(500);
    expect(task).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(500);
    expect(task).toHaveBeenCalledTimes(1);
    poller.stop();
  });

  test('sync task errors are swallowed (poll keeps running)', async () => {
    let calls = 0;
    const poller = usePolling(() => {
      calls += 1;
      if (calls <= 2) throw new Error('transient failure');
    }, { intervalMs: 100 });

    poller.start();
    await vi.advanceTimersByTimeAsync(300);
    expect(calls).toBe(4); // immediate + 3 ticks, no unhandled rejection
    poller.stop();
  });

  test('async task errors are swallowed too', async () => {
    const task = vi.fn().mockRejectedValueOnce(new Error('network down')).mockResolvedValue(undefined);
    const poller = usePolling(task, { intervalMs: 100 });

    poller.start();
    await vi.advanceTimersByTimeAsync(250);
    expect(task).toHaveBeenCalledTimes(3); // immediate + ticks at 100/200ms
    poller.stop();
  });
});