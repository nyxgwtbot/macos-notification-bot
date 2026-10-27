<template>
  <ion-page>
    <ion-header>
      <ion-toolbar class="nb-page-header">
        <ion-title>Logs</ion-title>
      </ion-toolbar>
    </ion-header>

    <ion-content class="nb-content nb-logs-content">
      <div class="nb-logs-layout">
        <div v-if="store.logsError" class="nb-banner nb-banner-err">
          <span>Failed to read the log: {{ store.logsError }}</span>
          <ion-button size="small" fill="outline" @click="refreshOnce">Retry</ion-button>
        </div>

        <div class="nb-log-header">
          <span class="nb-muted">{{ logTitle }}</span>
          <ion-badge :color="store.feedError ? 'danger' : 'primary'">
            <ion-icon aria-hidden="true" :icon="pulseOutline" />
            auto-reload 2s
          </ion-badge>
        </div>

        <p v-if="!store.logFileExists && !store.logsError" class="nb-empty">
          <code>logs/events.jsonl</code> doesn't exist yet — run the bot (<code>python3 bot.py</code>) or
          <code>python3 bot.py --once</code> to generate the first event.
        </p>

        <div ref="logScroller" class="nb-log-scroller" @scroll="onScroll">
          <div
            v-for="line in displayLines"
            :key="line.line_no"
            class="nb-line"
            :class="{ 'nb-line-bad': line.malformed, 'nb-line-hi': line.high }"
          >
            <button class="nb-copy" :aria-label="'Copy line ' + line.line_no" @click="copyLine(line.raw)">
              <ion-icon aria-hidden="true" :icon="copyOutline" />
            </button>
            <pre class="nb-raw">{{ line.raw || ' ' }}</pre>
            <span v-if="copiedAt === line.raw" class="nb-copied">Copied</span>
          </div>
          <div v-if="store.logLines.length === 0 && store.logFileExists" class="nb-empty">Log is empty.</div>
        </div>
        <p v-if="copiedAllText" class="nb-muted nb-copy-note">{{ copiedAllText }}</p>
      </div>
    </ion-content>
  </ion-page>
</template>

<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref } from 'vue';
import {
  IonBadge,
  IonButton,
  IonContent,
  IonHeader,
  IonIcon,
  IonPage,
  IonTitle,
  IonToolbar
} from '@ionic/vue';
import { copyOutline, pulseOutline } from 'ionicons/icons';

import { useBotStore } from '@/stores/bot';
import { usePolling } from '@/composables/usePolling';

const store = useBotStore();

const logScroller = ref<HTMLElement | null>(null);
const stickToBottom = ref(true);
const copiedAt = ref<string | null>(null);

const logTitle = computed(() => `Raw tail · ${store.logLines.length} line${store.logLines.length === 1 ? '' : 's'}`);
const copiedAllText = ref<string | null>(null);

/** L4: rows whose raw JSON carries "urgency": "high" get the warn treatment.
 * Pure view-layer line map — the tail payload/VM contract stays untouched. */
const URGENCY_HIGH_RE = /"urgency"\s*:\s*"high"/;
const displayLines = computed(() =>
  store.logLines.map((line) => ({
    ...line,
    high: URGENCY_HIGH_RE.test(line.raw)
  }))
);

/** Keep auto-scroll pinned to the bottom unless the user scrolled up. */
function onScroll(event: Event): void {
  const el = event.target as HTMLElement;
  if (!el) return;
  stickToBottom.value = el.scrollTop + el.clientHeight >= el.scrollHeight - 32;
}

async function copyLine(raw: string): Promise<void> {
  const text = raw ?? '';
  let copied = false;
  if (navigator.clipboard && window.isSecureContext) {
    try {
      await navigator.clipboard.writeText(text);
      copied = true;
    } catch (err) {
      console.debug('clipboard API failed', err);
    }
  }
  if (!copied) copied = copyViaTextarea(text);
  if (copied) {
    copiedAt.value = text;
    copiedAllText.value = null;
    window.setTimeout(() => (copiedAt.value = null), 1200);
  }
}

function copyViaTextarea(text: string): boolean {
  const helper = document.createElement('textarea');
  helper.value = text;
  helper.setAttribute('readonly', '');
  helper.style.position = 'fixed';
  helper.style.opacity = '0';
  document.body.appendChild(helper);
  helper.select();
  let ok = false;
  try {
    ok = document.execCommand('copy');
  } catch (err) {
    console.debug('execCommand copy failed', err);
    ok = false;
  } finally {
    document.body.removeChild(helper);
  }
  if (!ok) copiedAllText.value = 'Copy unavailable in this context.';
  return ok;
}

function refreshOnce(): void {
  void store.pollLogs();
}

const logPoller = usePolling(() => store.pollLogs(), { intervalMs: 2000 });

onMounted(async () => {
  await store.pollLogs();
  logPoller.start();
  await nextTick();
  stickToBottom.value = true;
});

onUnmounted(() => {
  logPoller.stop();
});
</script>

<style scoped>
.nb-logs-content {
  --overflow: hidden;
  --padding-top: var(--nb-space-4);
  --padding-start: var(--nb-space-4);
  --padding-end: var(--nb-space-4);
  --padding-bottom: var(--nb-space-4);
}

.nb-logs-layout {
  display: flex;
  flex-direction: column;
  height: 100%;
  min-height: 0;
}

.nb-log-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin: 0 0 var(--nb-space-2);
  flex-shrink: 0;
}

.nb-log-scroller {
  flex: 1 1 0;
  min-height: 0;
  overflow-y: auto;
  border: 1px solid var(--nb-border-default);
  border-radius: var(--nb-radius-md);
  background: var(--nb-bg-deep);
  padding: var(--nb-space-2) var(--nb-space-1);
  /* L2: micro row gap between log lines */
  display: flex;
  flex-direction: column;
  gap: var(--nb-space-1);
}

/* L3: every row is [icon gutter][content column]; wrapped continuation lines
   inside .nb-raw start at the same content x — the hanging indent. */
.nb-line {
  position: relative;
  display: flex;
  align-items: flex-start;
  gap: var(--nb-space-1);
  /* uniform left border slot; L4 re-colors it instead of shifting layout */
  border-left: 2px solid transparent;
}

.nb-line-bad .nb-raw {
  opacity: 0.38;
  font-style: italic;
}

/* L4: warn marker + bg tint on urgency:high rows */
.nb-line-hi {
  border-left-color: var(--nb-warning);
  background: var(--nb-warning-muted);
}

.nb-raw {
  flex: 1 1 auto;
  min-width: 0;
  margin: 0;
  padding: var(--nb-space-1) var(--nb-space-1) var(--nb-space-1) var(--nb-space-2);
  font-family: var(--nb-mono);
  font-size: var(--nb-fs-micro);
  /* L2: relaxed line height (was fs 11/1.3-1.45 wall) */
  line-height: 1.5;
  white-space: pre-wrap;
  word-break: break-all;
  color: var(--nb-text-primary);
}

/* L1: the copy affordance is noise at rest — reveal on hover/keyboard focus */
.nb-copy {
  background: none;
  border: none;
  color: var(--nb-text-secondary);
  cursor: pointer;
  padding: 2px var(--nb-space-1); /* 2px micro-inner exception */
  margin-top: 2px;
  opacity: 0;
  transition: opacity 120ms ease;
}
.nb-line:hover .nb-copy,
.nb-copy:focus-visible {
  opacity: 1;
}

.nb-copied {
  position: relative;
  right: 0;
  top: 3px;
  font-size: var(--nb-fs-micro);
  color: var(--ion-color-success);
}

.nb-copy-note {
  margin-top: var(--nb-space-2);
  font-size: var(--nb-fs-secondary);
  flex-shrink: 0;
}
</style>
