/**
 * Interval polling composable: start/stop, immediate-first-run option,
 * fake-timer friendly (plain global setInterval; vitest fake timers replace
 * jsdom's). Mirrors the timer discipline of hydranote's useIdleAutoSave.spec.
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

  function start(): void {
    if (isActive.value || timer !== null) return;
    isActive.value = true;
    if (options.immediate !== false) {
      void runOnce();
    }
    timer = window.setInterval(() => {
      void runOnce();
    }, options.intervalMs);
  }

  function stop(): void {
    isActive.value = false;
    if (timer !== null) {
      window.clearInterval(timer);
      timer = null;
    }
  }

  return { isActive: readonly(isActive) as ReturnType<typeof ref<boolean>>, start, stop };
}