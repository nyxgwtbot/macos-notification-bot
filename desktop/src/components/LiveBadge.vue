<template>
  <!-- R8 shared live badge — ONE wording for every poll cadence chip: the
       label is always "Live" and the native tooltip carries the real cadence
       ("Refreshes every 2 s"), read from the consumer's poll config so the
       badge can never drift from the actual usePolling interval. The poll's
       error state flips the tint to danger (wording stays "Live"; the
       consumers' own error banners carry the failure text + retry). -->
  <span
    class="nb-live-badge nb-status-chip"
    :class="toneClass"
    :data-state="dataState"
    :title="tooltip"
  >
    <i class="nb-dot" aria-hidden="true" />
    Live
  </span>
</template>

<script setup lang="ts">
import { computed } from 'vue';

const props = defineProps<{
  /** Poll cadence from the consumer's usePolling config (ms) — the tooltip
   * is derived from it ("Refreshes every 2 s" for 2000). */
  intervalMs: number;
  /** Current poll error text; a non-null value shows the danger tint. */
  error?: string | null;
  /** Degraded poll state (e.g. slow/reconnecting); shows the warning tint. */
  degraded?: boolean | string | null;
}>();

const dataState = computed(() => {
  if (props.error) return 'error';
  if (props.degraded) return 'degraded';
  return 'live';
});

const toneClass = computed(() => {
  if (props.error) return 'nb-status-bad';
  if (props.degraded) return 'nb-status-warn';
  return 'nb-status-live';
});

const seconds = computed(() => props.intervalMs / 1000);
const tooltip = computed(() => `Refreshes every ${seconds.value} s`);
</script>
