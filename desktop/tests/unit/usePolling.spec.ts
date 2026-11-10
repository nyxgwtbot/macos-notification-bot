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

/* ========================================
   R8 VISIBILITY PAUSE — the cadence sleeps while the page is hidden and
   becoming visible fires ONE immediate catch-up refresh before the interval
   resumes (every polling consumer inherits). Simulated here by controlling
   document.hidden and dispatching the same visibilitychange event the
   browser fires.
   ======================================== */
describe('usePolling visibility pause (R8)', () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    setHidden(false); // restore the jsdom default for the next spec
    vi.useRealTimers();
  });

  function setHidden(hidden: boolean): void {
    Object.defineProperty(document, 'hidden', { configurable: true, get: () => hidden });
  }
  function fireVisibilityChange(): void {
    document.dispatchEvent(new Event('visibilitychange'));
  }

  test('hidden pauses the cadence; visible fires an immediate refresh + resumes', async () => {
    const task = vi.fn().mockResolvedValue(undefined);
    const poller = usePolling(task, { intervalMs: 2000 });
    poller.start();
    await vi.advanceTimersByTimeAsync(0);
    expect(task).toHaveBeenCalledTimes(1); // immediate first run

    // hidden: the timer is dropped — advancing the clock runs nothing
    setHidden(true);
    fireVisibilityChange();
    await vi.advanceTimersByTimeAsync(10000);
    expect(task).toHaveBeenCalledTimes(1);

    // visible: ONE catch-up refresh right away, then the interval resumes
    setHidden(false);
    fireVisibilityChange();
    await vi.advanceTimersByTimeAsync(0);
    expect(task).toHaveBeenCalledTimes(2);
    await vi.advanceTimersByTimeAsync(2000);
    expect(task).toHaveBeenCalledTimes(3);
    await vi.advanceTimersByTimeAsync(8000);
    expect(task).toHaveBeenCalledTimes(7); // 2 + catch-up + 4 ticks (2s each)

    poller.stop();
  });

  test('started while hidden: first run still happens, then sleeps until visible', async () => {
    const task = vi.fn().mockResolvedValue(undefined);
    setHidden(true);
    const poller = usePolling(task, { intervalMs: 1000 });
    poller.start();
    await vi.advanceTimersByTimeAsync(0);
    expect(task).toHaveBeenCalledTimes(1); // the view still gets its initial paint
    await vi.advanceTimersByTimeAsync(10000);
    expect(task).toHaveBeenCalledTimes(1); // cadence asleep

    setHidden(false);
    fireVisibilityChange();
    await vi.advanceTimersByTimeAsync(0);
    expect(task).toHaveBeenCalledTimes(2); // catch-up refresh on visible
    poller.stop();
  });

  test('stop detaches the visibility listener (no refresh after unmount)', async () => {
    const task = vi.fn().mockResolvedValue(undefined);
    const poller = usePolling(task, { intervalMs: 100 });

    poller.start();
    await vi.advanceTimersByTimeAsync(0);
    expect(task).toHaveBeenCalledTimes(1);
    poller.stop();

    setHidden(false);
    fireVisibilityChange(); // late event must not resurrect the task
    await vi.advanceTimersByTimeAsync(5000);
    expect(task).toHaveBeenCalledTimes(1);
  });

  test('a hidden -> hidden toggle does not fire extra refreshes', async () => {
    const task = vi.fn().mockResolvedValue(undefined);
    const poller = usePolling(task, { intervalMs: 100 });
    poller.start();
    await vi.advanceTimersByTimeAsync(0);
    expect(task).toHaveBeenCalledTimes(1);

    setHidden(true);
    fireVisibilityChange();
    fireVisibilityChange(); // an extra hidden->hidden cycle
    await vi.advanceTimersByTimeAsync(1000);
    expect(task).toHaveBeenCalledTimes(1);
    poller.stop();
  });
});