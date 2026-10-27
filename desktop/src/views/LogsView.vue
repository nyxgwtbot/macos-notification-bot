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
          <div class="nb-log-header-row">
            <span class="nb-muted">{{ logTitle }}</span>
            <div class="nb-log-header-end">
              <ion-badge v-if="!followOn && newCount > 0" color="secondary">+{{ newCount }} new</ion-badge>
              <ion-badge :color="store.feedError ? 'danger' : 'primary'">
                <ion-icon aria-hidden="true" :icon="pulseOutline" />
                auto-reload 2s
              </ion-badge>
            </div>
          </div>
          <div class="nb-log-controls">
            <div class="nb-filter-chips" role="group" aria-label="Filter log lines by record kind">
              <button
                v-for="option in LOG_KIND_FILTER_OPTIONS"
                :key="option.value"
                type="button"
                class="nb-filter-chip"
                :data-active="kindFilter === option.value"
                :aria-pressed="kindFilter === option.value"
                @click="onKindFilterChange(option.value)"
              >
                {{ option.label }}
              </button>
            </div>
            <button
              type="button"
              class="nb-follow-chip"
              :data-active="followOn"
              :aria-pressed="followOn"
              :aria-label="followOn ? 'Following the tail — click to pause' : 'Paused — click to follow the tail again'"
              @click="onFollowChipClick"
            >
              {{ followOn ? 'Follow' : 'Paused — follow?' }}
            </button>
            <button
              type="button"
              class="nb-copy-all"
              aria-label="Copy visible tail text"
              @click="copyAll"
            >
              <ion-icon aria-hidden="true" :icon="copyOutline" />
              Copy all
            </button>
          </div>
        </div>

        <p v-if="!store.logFileExists && !store.logsError" class="nb-empty">
          <code>logs/events.jsonl</code> doesn't exist yet — run the bot (<code>python3 bot.py</code>) or
          <code>python3 bot.py --once</code> to generate the first event.
        </p>

        <div ref="logScroller" class="nb-log-scroller" @scroll="onScroll">
          <div
            v-for="line in visibleLines"
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
          <div v-if="visibleLines.length === 0 && store.logFileExists" class="nb-empty">No matching lines.</div>
        </div>
        <p v-if="copiedAllText" class="nb-muted nb-copy-note">{{ copiedAllText }}</p>
      </div>
    </ion-content>
  </ion-page>
</template>

<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue';
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
import { copyText } from '@/utils/clipboard';
import { filterLogLines, LOG_KIND_FILTER_OPTIONS, type LogKindFilter } from '@/viewModels/logsViewModel';

const store = useBotStore();

const logScroller = ref<HTMLElement | null>(null);
/** Follow-tail control: ON pins the scroller to the bottom; a manual scroll
 * up pauses it (chip switches to "Paused — follow?"); clicking re-enables. */
const followOn = ref(true);
const newCount = ref(0);
/** Raw lines already accounted for while paused (line_no is window-relative
 * and shifts as the file grows, so raws are the only stable identity). */
let seenRaws: Set<string> | null = null;
/** Guard: our own smooth scroll-to-bottom would read as "scrolled up". */
let programmaticScrollThrough = false;
let programmaticScrollTimer: number | null = null;
// Programmatic smooth scroll runs ~300ms; the guard window covers it.
const PROGRAMMATIC_SCROLL_GUARD_MS = 500;
const NEAR_BOTTOM_PX = 32;

const copiedAt = ref<string | null>(null);
const copiedAllText = ref<string | null>(null);
const copiedAllTimer: { value: number | null } = { value: null };
let copiedTimer: number | null = null;

const kindFilter = ref<LogKindFilter>('all');

const logTitle = computed(() =>
  `Raw tail · ${visibleLines.value.length} line${visibleLines.value.length === 1 ? '' : 's'}`
);

/** L4: rows whose raw JSON carries "urgency": "high" get the warn treatment.
 * Pure view-layer line map — the tail payload/VM contract stays untouched.
 * Kind filter (iterB) applies first, client-side, so the highlight map sees
 * the filtered lines. */
const URGENCY_HIGH_RE = /"urgency"\s*:\s*"high"/;
const visibleLines = computed(() =>
  filterLogLines(store.logLines, kindFilter.value).map((line) => ({
    ...line,
    high: URGENCY_HIGH_RE.test(line.raw)
  }))
);

function onKindFilterChange(kind: LogKindFilter): void {
  if (kindFilter.value === kind) return;
  kindFilter.value = kind;
  rebaseNewCount();
}

/** Keep auto-scroll pinned to the bottom unless the user scrolled up; a scroll
 * up pauses follow (chip -> "Paused — follow?"), the chip click re-enables. */
function onScroll(event: Event): void {
  const el = event.target as HTMLElement;
  if (!el) return;
  if (programmaticScrollThrough) return;
  if (!isNearBottom(el) && followOn.value) pauseFollow();
}

function isNearBottom(el: HTMLElement): boolean {
  return el.scrollTop + el.clientHeight >= el.scrollHeight - NEAR_BOTTOM_PX;
}

async function scrollToBottom(behavior: ScrollBehavior): Promise<void> {
  await nextTick();
  const el = logScroller.value;
  if (!el) return;
  const distance = el.scrollHeight - el.scrollTop;
  programmaticScrollThrough = distance > 0;
  try {
    el.scrollTo({ top: el.scrollHeight, behavior });
  } catch {
    el.scrollTop = el.scrollHeight; // pre-`scrollTo` engines
  }
  if (programmaticScrollTimer !== null) window.clearTimeout(programmaticScrollTimer);
  programmaticScrollTimer = window.setTimeout(() => {
    programmaticScrollThrough = false;
  }, PROGRAMMATIC_SCROLL_GUARD_MS);
}

function pauseFollow(): void {
  followOn.value = false;
  seenRaws = new Set(store.logLines.map((line) => line.raw));
  newCount.value = 0;
}

function enableFollow(): void {
  followOn.value = true;
  seenRaws = null;
  newCount.value = 0;
  void scrollToBottom('smooth');
}

function onFollowChipClick(): void {
  if (followOn.value) {
    pauseFollow();
  } else {
    enableFollow();
  }
}

/** While paused, fetched lines still append; the badge counts the ones below
 * the fold (raws not in the pause-time snapshot, counted once each). */
function countUnseen(): number {
  if (!seenRaws) return 0;
  let unseen = 0;
  for (const line of store.logLines) {
    if (!seenRaws.has(line.raw)) {
      unseen += 1;
      seenRaws.add(line.raw);
    }
  }
  return unseen;
}

/** Fresh baseline when the user changes the kind filter mid-pause. */
function rebaseNewCount(): void {
  if (!followOn.value) {
    seenRaws = new Set(visibleLines.value.map((line) => line.raw));
    newCount.value = 0;
  }
}

watch(
  () => store.logLines,
  () => {
    if (followOn.value) {
      void scrollToBottom('smooth');
    } else {
      newCount.value = countUnseen();
    }
  }
);

/** Transient "Copied" affordance for a single copied line. */
function markLineCopied(raw: string): void {
  copiedAt.value = raw;
  if (copiedTimer !== null) window.clearTimeout(copiedTimer);
  copiedTimer = window.setTimeout(() => (copiedAt.value = null), 1200);
}

async function copyLine(raw: string): Promise<void> {
  if (await copyText(raw ?? '')) {
    copiedAllText.value = null;
    markLineCopied(raw ?? '');
  } else {
    copiedAllText.value = 'Copy unavailable in this context.';
  }
}

/** Copy all: the current visible (kind-filtered) tail text, one line per row. */
async function copyAll(): Promise<void> {
  const text = visibleLines.value.map((line) => line.raw).join('\n');
  if (await copyText(text)) {
    copiedAt.value = null;
    if (copiedAllTimer.value !== null) window.clearTimeout(copiedAllTimer.value);
    copiedAllText.value = `Copied ${visibleLines.value.length} line${visibleLines.value.length === 1 ? '' : 's'}`;
    copiedAllTimer.value = window.setTimeout(() => (copiedAllText.value = null), 1200);
  } else {
    copiedAllText.value = 'Copy unavailable in this context.';
  }
}

function refreshOnce(): void {
  void store.pollLogs();
}

const logPoller = usePolling(() => store.pollLogs(), { intervalMs: 2000 });

onMounted(async () => {
  await store.pollLogs();
  logPoller.start();
  await nextTick();
  // first paint pins to the tail instantly (no smooth animation)
  const el = logScroller.value;
  if (el) el.scrollTop = el.scrollHeight;
  followOn.value = true;
});

onUnmounted(() => {
  logPoller.stop();
  if (programmaticScrollTimer !== null) window.clearTimeout(programmaticScrollTimer);
  if (copiedTimer !== null) window.clearTimeout(copiedTimer);
  if (copiedAllTimer.value !== null) window.clearTimeout(copiedAllTimer.value);
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
  flex-direction: column;
  gap: var(--nb-space-2);
  margin: 0 0 var(--nb-space-2);
  flex-shrink: 0;
}

.nb-log-header-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--nb-space-2);
}

.nb-log-header-end {
  display: inline-flex;
  align-items: center;
  gap: var(--nb-space-2);
}

/* follow chip: same tinted-chip DNA as the shared filter chips; the tealed
   state = following (active/pinned), neutral = paused */
.nb-follow-chip {
  display: inline-flex;
  align-items: center;
  padding: 2px var(--nb-space-2); /* 2px micro-inner exception */
  border-radius: var(--nb-radius-sm);
  font-size: var(--nb-fs-micro);
  font-weight: 600;
  line-height: 1.45;
  white-space: nowrap;
  border: 1px solid transparent;
  background: var(--nb-bg-elevated);
  color: var(--nb-text-secondary);
  cursor: pointer;
}
.nb-follow-chip:hover {
  background: var(--nb-bg-hover);
}
.nb-follow-chip[data-active='true'] {
  background: var(--nb-info-muted);
  color: var(--nb-info);
}

/* copy all: quiet ghost affordance in the header, hover brightens like the
   line-copy buttons */
.nb-copy-all {
  display: inline-flex;
  align-items: center;
  gap: var(--nb-space-1);
  padding: 2px var(--nb-space-1) 2px var(--nb-space-2); /* 2px micro-inner exception */
  background: none;
  border: 1px solid var(--nb-border-default);
  border-radius: var(--nb-radius-sm);
  color: var(--nb-text-secondary);
  font-size: var(--nb-fs-micro);
  font-weight: 600;
  line-height: 1.45;
  white-space: nowrap;
  cursor: pointer;
}
.nb-copy-all:hover,
.nb-copy-all:focus-visible {
  color: var(--nb-text-primary);
  border-color: var(--nb-border-strong);
}

.nb-log-controls {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--nb-space-2);
  flex-wrap: wrap;
}

.nb-log-controls .nb-copy-all {
  margin-left: auto;
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
