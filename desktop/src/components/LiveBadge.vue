<template>
  <!-- R8 shared live badge — ONE wording for every poll cadence chip: the
       label is always "Live" and the native tooltip carries the real cadence
       ("Refreshes every 2 s"), read from the consumer's poll config so the
       badge can never drift from the actual usePolling interval. The poll's
       error state flips the tint to danger (wording stays "Live"; the
       consumers' own error banners carry the failure text + retry). -->
  <ion-badge class="nb-live-badge" :data-state="error ? 'error' : 'live'" :color="tone" :title="tooltip">
    <ion-icon aria-hidden="true" :icon="pulseOutline" />
    Live
  </ion-badge>
</template>

<script setup lang="ts">
import { computed } from 'vue';
import { IonBadge, IonIcon } from '@ionic/vue';
import { pulseOutline } from 'ionicons/icons';

const props = defineProps<{
  /** Poll cadence from the consumer's usePolling config (ms) — the tooltip
   * is derived from it ("Refreshes every 2 s" for 2000). */
  intervalMs: number;
  /** Current poll error text; a non-null value shows the danger tint. */
  error?: string | null;
}>();

const tone = computed(() => (props.error ? 'danger' : 'primary'));

const seconds = computed(() => props.intervalMs / 1000);
const tooltip = computed(() => `Refreshes every ${seconds.value} s`);
</script>