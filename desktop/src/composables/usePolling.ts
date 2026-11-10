/**
 * Interval polling composable: start/stop, immediate-first-run option,
 * fake-timer friendly (plain global setInterval; vitest fake timers replace
 * jsdom's). Mirrors the timer discipline of hydranote's useIdleAutoSave.spec.
 * R8: the cadence sleeps while the page is hidden (visibilitychange) and
 * becoming visible fires ONE immediate refresh before the interval resumes —
 * every polling consumer inherits the pause, and the catch-up run keeps the
 * consumers' age recomputation honest (nowMs/relText refresh with it).
 */

import { readonly, ref } from 'vue';

export interface UsePollingHandle {
  isActive: ReturnType<typeof ref<boolean>>;
  start: () => void;
  stop: () => void;
}

export interface UsePollingOptions {
  intervalMs: number;
  /** Run the task right when starting (default true). */
  immediate?: boolean;
}

export function usePolling(task: () => void | Promise<void>, options: UsePollingOptions): UsePollingHandle {
  let timer: number | null = null;
  const isActive = ref(false);

  async function runOnce(): Promise<void> {
    try {
      await task();
    } catch (err) {
      // Polling tasks own their error state; this keeps a rejected async task
      // from surfacing as an unhandled event loop error.
      console.debug('[usePolling] task error', err);
    }
  }

  /** (Re)arm the interval; a pending one is cleared first (no double timers). */
  function restartTimer(): void {
    clearTimer();
    timer = window.setInterval(() => {
      void runOnce();
    }, options.intervalMs);
  }

  function clearTimer(): void {
    if (timer !== null) {
      window.clearInterval(timer);
      timer = null;
    }
  }

  /**
   * R8 visibility pause: hidden pages drop the interval (no background
   * HTTP noise, no wasted ticks) while stays-active; visibility keeps the
   * handle. On visible, ONE immediate catch-up refresh fires before the
   * interval resumes — the consumers' poll tasks recompute their age clocks
   * (Dashboard nowMs), so timestamps stay honest right after the resume.
   */
  function onVisibilityChange(): void {
    if (!isActive.value) return;
    clearTimer();
    if (document.hidden) return;
    void runOnce();
    restartTimer();
  }

  function start(): void {
    if (isActive.value || timer !== null) return;
    isActive.value = true;
    document.addEventListener('visibilitychange', onVisibilityChange);
    if (options.immediate !== false) {
      void runOnce();
    }
    restartTimer();
    // Started while hidden: the immediate first run already happened (the
    // view needs its initial paint); the cadence sleeps until visible.
    if (document.hidden) clearTimer();
  }

  function stop(): void {
    isActive.value = false;
    document.removeEventListener('visibilitychange', onVisibilityChange);
    clearTimer();
  }

  return { isActive: readonly(isActive) as ReturnType<typeof ref<boolean>>, start, stop };
}