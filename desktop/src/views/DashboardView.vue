<template>
  <ion-page>
    <ion-header>
      <ion-toolbar class="nb-page-header">
        <ion-title>Status</ion-title>
        <!-- S2: page header row carries the feed liveness badge inline (title at
             fs-title-lg + badge on ONE row; shared pattern lands in X1) -->
        <div slot="end" class="nb-head-end">
          <ion-badge :color="store.feedError ? 'danger' : 'primary'">
            <ion-icon aria-hidden="true" :icon="pulseOutline" />
            {{ store.feedError ? 'offline' : 'every 2s' }}
          </ion-badge>
        </div>
      </ion-toolbar>
    </ion-header>

    <ion-content class="nb-content">
      <!-- offline banner: API down -> explicit banner + retry, never blank -->
      <div v-if="store.apiDown" class="nb-offline">
        <ion-spinner v-if="store.feedLoading" name="crescent" />
        <ion-icon aria-hidden="true" :icon="cloudOfflineOutline" />
        <span>API offline: {{ store.feedError }}</span>
        <ion-button size="small" fill="outline" @click="retryPolls">Retry</ion-button>
      </div>

      <div class="nb-cards">
        <!-- Daemon card: process managed by Electron main (not via the API) -->
        <ion-card>
          <ion-card-header class="nb-card-head">
            <ion-card-title>Daemon</ion-card-title>
            <!-- S1: compact status chip (dot + label) right-aligned, not a full-width pill -->
            <span class="nb-status-chip" :class="'nb-status-' + daemonChipTone">
              <i class="nb-dot" aria-hidden="true"></i>{{ daemonBadgeText }}
            </span>
          </ion-card-header>
          <ion-card-content>
            <template v-if="store.daemon">
              <div class="nb-kv"><span>PID</span><strong>{{ store.daemon.pid ?? '—' }}</strong></div>
              <div class="nb-kv"><span>Uptime</span><strong>{{ formatUptime(store.daemon.uptimeSec) || '—' }}</strong></div>
              <!-- R2: the stamp stays the primary value, with the live age as
                   a fs-micro muted secondary hanging under it -->
              <div class="nb-kv">
                <span>Started</span>
                <span class="nb-kv-stack">
                  <strong :title="formatIsoShort(store.daemon.startedAt) || undefined">{{ formatIsoShort(store.daemon.startedAt) || '—' }}</strong>
                  <span v-if="daemonStartedAge" class="nb-age-secondary">{{ daemonStartedAge }}</span>
                </span>
              </div>
              <div class="nb-kv"><span>API port</span><strong>{{ apiPortDisplay }}</strong></div>
              <div class="nb-kv"><span>Adopted</span><strong>{{ store.daemon.adopted ? 'yes (external instance)' : 'no (managed here)' }}</strong></div>
              <div v-if="store.daemon.lastError" class="nb-error-line">Error: {{ store.daemon.lastError }}</div>
            </template>
            <template v-else>
              <p class="nb-muted">
                {{ store.daemonError === 'bridge-ausente' ? 'Process manager unavailable in browser mode (vite). Manage the daemon via `python3 bot.py`.' : store.daemonError || 'No daemon status.' }}
              </p>
            </template>
            <div class="nb-card-footer">
              <!-- dangerous action made hard: two clicks 3s apart; click 1 arms,
                   expiry disarms silently, only click 2 restarts -->
              <ion-button
                size="small"
                fill="outline"
                class="nb-restart-btn"
                :class="{ 'nb-restart-armed': restartArmed }"
                :disabled="store.daemonBusy || !daemonBridge"
                @click="onRestartDaemon"
              >
                <ion-spinner v-if="store.daemonBusy" name="crescent" />
                <template v-else>{{ restartArmed ? 'Confirm restart?' : 'Restart daemon' }}</template>
              </ion-button>
            </div>
          </ion-card-content>
        </ion-card>

        <!-- Health card: watermark + state + ollama (from GET /api/health) -->
        <ion-card>
          <ion-card-header class="nb-card-head">
            <ion-card-title>Bot state</ion-card-title>
            <span class="nb-status-chip" :class="'nb-status-' + healthChipTone">
              <i class="nb-dot" aria-hidden="true"></i>{{ store.healthError ? 'error' : 'ok' }}
            </span>
          </ion-card-header>
          <ion-card-content>
            <template v-if="store.health">
              <div class="nb-kv"><span title="last_rec_id">Watermark</span><strong>{{ store.health.watermark.last_rec_id }}</strong></div>
              <!-- R2: the AGE is the live value (recomputed on each 2s poll
                   tick via nowMs); the absolute stamp survives as tooltip -->
              <div class="nb-kv">
                <span title="state.updated_at">Last cycle</span>
                <strong :title="formatIsoShort(store.health.watermark.updated_at) || undefined">{{ lastCycleAge || '—' }}</strong>
              </div>
              <div class="nb-kv"><span>Ollama</span><strong>{{ store.health.ollama.url }}</strong></div>
              <div class="nb-kv"><span>Model</span><strong>{{ store.health.ollama.model }}</strong></div>
              <!-- R2 split: the log size is one row (value only), versions
                   read as their own row instead of a cryptic cram suffix -->
              <div class="nb-kv"><span>Log</span><strong>{{ formatBytes(store.health.log.bytes) }}</strong></div>
              <div class="nb-kv"><span>Version</span><strong>{{ store.health.bot_version }} (api v{{ store.health.api_version }})</strong></div>
              <div class="nb-kv"><span>Finance</span><strong>{{ store.health.finance.enabled ? (store.health.finance.db_exists ? 'enabled' : 'enabled (no ledger)') : 'disabled' }}</strong></div>
            </template>
            <template v-else>
              <p class="nb-muted">
                <ion-spinner v-if="store.healthLoading" name="crescent" />
                <span v-else>No response from /api/health{{ store.healthError ? `: ${store.healthError}` : '…' }}</span>
              </p>
            </template>
          </ion-card-content>
        </ion-card>
      </div>

      <!-- Live feed -->
      <div class="nb-feed-header">
        <h2>Live events</h2>
        <!-- feed kind filter chips: they filter ONLY this feed (server-side
             `kind=` on /api/events, full reload per change), so they live on
             the feed header row, not at the page top; shared .nb-filter-chips
             container wraps them on narrow widths -->
        <div class="nb-filter-chips" role="group" aria-label="Filter events by kind">
          <button
            v-for="option in EVENT_KIND_OPTIONS"
            :key="option.value"
            type="button"
            class="nb-filter-chip"
            :data-active="store.feedKind === option.value"
            :aria-pressed="store.feedKind === option.value"
            @click="onKindChange(option.value)"
          >
            {{ option.label }}
          </button>
        </div>
      </div>

      <!-- R2 finding 4: all-startup feed cue — the filtered feed has zero
           macOS notification events yet (startup lines only), so the user
           hasn't seen the full notification flow; static EN copy -->
      <p v-if="showStartupHint" class="nb-feed-hint">
        Nothing from macOS yet — send a test notification from Chat to see the full flow.
      </p>

      <ion-spinner v-if="store.feedLoading && store.events.length === 0" name="crescent" class="nb-center" />

      <p v-else-if="!store.apiDown && store.events.length === 0" class="nb-empty">
        No events yet — the bot hasn't recorded a notification. Ask the bot in <strong>Chat</strong>, or wait for a macOS event.
      </p>

      <div v-if="store.events.length > 0" class="nb-feed">
        <div
          v-for="row in feedRows"
          :key="row.key"
          class="nb-event"
          :class="{ 'nb-event-startup': row.kind === 'startup', 'nb-event-open': expandedRows.has(row.key) }"
          role="button"
          tabindex="0"
          :aria-expanded="expandedRows.has(row.key)"
          :aria-label="'Expand raw payload for ' + (row.app || row.kind)"
          @click="toggleExpand(row.key)"
          @keydown.enter.prevent="toggleExpand(row.key)"
        >
          <div class="nb-event-top">
            <ion-icon class="nb-event-chev" aria-hidden="true" :icon="chevronForwardOutline" />
            <!-- kind identity: leading tinted icon chip; R2 finding 1 — the
                 collapsed row reads as chevron + icon + tag + timestamps and
                 the per-row source label lives ONLY in the expanded inset -->
            <span class="nb-kind" :class="'nb-kind-' + eventKindIdentity(row)" aria-hidden="true">
              <ion-icon :icon="KIND_ICONS[eventKindIdentity(row)]" />
            </span>
            <!-- startup rows: muted kind tag next to the icon is the whole
                 "summary" (startup events carry no body/summary fields) -->
            <span v-if="row.kind === 'startup'" class="nb-kind-caption">startup</span>
            <span class="nb-when">{{ formatIsoShort(row.timestamp) }}</span>
            <!-- relative age recomputed on every 2s feed poll tick -->
            <span v-if="row.relText" class="nb-when nb-when-rel">{{ row.relText }}</span>
            <span v-if="row.recId !== null" class="nb-recid">rec_id {{ row.recId }}</span>
          </div>
          <!-- R2 finding 1: the repeated bold kind/title line is gone from the
               collapsed row — notifications speak with their classifier
               summary (better signal); the label survives as the micro
               caption in the expanded raw inset -->
          <template v-if="row.kind === 'notification'">
            <div class="nb-event-summary">{{ row.summary }}</div>
            <div class="nb-chips">
              <ion-chip :color="categoryColor">{{ row.category }}</ion-chip>
              <ion-chip :color="urgencyColor(row.urgency)">{{ row.urgency }}</ion-chip>
              <ion-badge v-if="row.layer" :color="layerBadgeColor(row.layer)" class="nb-badge">{{ row.layerLabel }}</ion-badge>
              <ion-badge v-if="row.ruleMatched" color="secondary" class="nb-badge">rule: {{ row.ruleMatched }}</ion-badge>
              <ion-badge v-if="row.confidencePct" color="tertiary" class="nb-badge">confidence {{ row.confidencePct }}</ion-badge>
              <ion-badge v-if="row.telegramStatus" :color="telegramBadgeColor(row.telegramStatus)" class="nb-badge">telegram {{ row.telegramStatus }}</ion-badge>
              <ion-badge v-if="row.financeStatus" :color="financeBadgeColor(row.financeStatus)" class="nb-badge">finance {{ row.financeStatus }}</ion-badge>
            </div>
            <div v-if="row.actions.length > 0" class="nb-actions">
              <span v-for="action in row.actions" :key="action" class="nb-action">{{ action }}</span>
            </div>
          </template>
          <!-- expand: raw event payload, pretty-printed, 2 KB tail-trim, full copy on hover -->
          <div
            v-if="expandedRows.has(row.key)"
            class="nb-event-raw"
            role="region"
            :aria-label="'Raw payload for ' + (row.app || row.kind)"
            @click.stop
          >
            <button class="nb-raw-copy" aria-label="Copy raw payload" @click="copyRaw(row)">
              <ion-icon aria-hidden="true" :icon="copyOutline" />
              <span v-if="copiedKey === row.key" class="nb-raw-copied">Copied</span>
            </button>
            <!-- the row label (source app + notification title) lives only in
                 the expanded view now, as a micro caption above the raw JSON -->
            <span v-if="expandedRowCaption(row)" class="nb-event-raw-source">{{ expandedRowCaption(row) }}</span>
            <pre class="nb-event-raw-text">{{ rawPreview(row).text }}</pre>
            <span v-if="rawPreview(row).truncatedChars > 0" class="nb-event-raw-note">
              … {{ rawPreview(row).truncatedChars }} chars trimmed (2 KB display cap) — copy takes the full payload
            </span>
          </div>
        </div>
      </div>
    </ion-content>
  </ion-page>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, reactive, ref } from 'vue';
import {
  IonBadge,
  IonButton,
  IonCard,
  IonCardContent,
  IonCardHeader,
  IonCardTitle,
  IonChip,
  IonContent,
  IonHeader,
  IonIcon,
  IonPage,
  IonSpinner,
  IonTitle,
  IonToolbar
} from '@ionic/vue';
import { cashOutline, chevronForwardOutline, cloudOfflineOutline, copyOutline, notificationsOutline, pulseOutline, timeOutline } from 'ionicons/icons';

import { useBotStore } from '@/stores/bot';
import { usePolling } from '@/composables/usePolling';
import { copyText } from '@/utils/clipboard';
import { formatBytes, formatIsoShort, formatUptime } from '@/utils/time';
import { ageLabel, formatApiPort } from '@/viewModels/statusViewModel';
import {
  EVENT_KIND_OPTIONS,
  eventKindIdentity,
  expandedRowCaption,
  feedIsAllStartup,
  payloadPreview,
  relativeTimeLabel,
  type EventKindIdentity,
  type EventKindValue,
  type EventRow
} from '@/viewModels/eventViewModel';

const store = useBotStore();

const daemonBridge = computed(() => typeof window !== 'undefined' && !!window.notifyAPI?.daemon);

/** Kind identity -> leading icon (startup: clock, notification: bell,
 * finance: cash). The identity itself is a pure view-model helper. */
const KIND_ICONS: Record<EventKindIdentity, string> = {
  startup: timeOutline,
  notification: notificationsOutline,
  finance: cashOutline
};

/** Feed row plus the relative-age caption; recomputed against nowMs, which
 * the 2s feed poller bumps every tick. */
type FeedRow = EventRow & { relText: string };
const nowMs = ref<number>(Date.now());
const feedRows = computed<FeedRow[]>(() =>
  [...store.events]
    .reverse()
    .map((row) => ({ ...row, relText: relativeTimeLabel(row.timestamp, nowMs.value) }))
);

/** R2 finding 2: realtime card ages — recomputed on every 2s feed tick
 * (nowMs), so "4s ago" -> "12s ago" reads live although health itself
 * refreshes on the slower 5s loop. */
const lastCycleAge = computed(() => ageLabel(store.health?.watermark.updated_at ?? '', nowMs.value));
const daemonStartedAge = computed(() => ageLabel(store.daemon?.startedAt ?? '', nowMs.value));

/** R2 finding 4: the all-startup cue renders whenever the filtered feed is
 * non-empty and carries zero notification-kind rows (API down keeps the
 * offline banner in charge; an empty feed keeps the plain empty state). */
const showStartupHint = computed(() => !store.apiDown && feedIsAllStartup(store.events));

/** Event expand state: per-row key, component-local (like the chat tool chips). */
const expandedRows = reactive(new Set<string>());
const copiedKey = ref<string | null>(null);

function toggleExpand(key: string): void {
  if (expandedRows.has(key)) {
    expandedRows.delete(key);
  } else {
    expandedRows.add(key);
  }
}

function rawPreview(row: EventRow) {
  return payloadPreview(row.raw);
}

async function copyRaw(row: EventRow): Promise<void> {
  try {
    const text = JSON.stringify(row.raw, null, 2);
    if (await copyText(text)) {
      copiedKey.value = row.key;
      window.setTimeout(() => {
        if (copiedKey.value === row.key) copiedKey.value = null;
      }, 1200);
    }
  } catch (err) {
    console.debug('payload copy failed', err);
  }
}

function onKindChange(kind: EventKindValue): void {
  void store.setFeedKind(kind);
}

const apiPortDisplay = computed(() =>
  formatApiPort({
    daemonPort: store.daemon?.port,
    adopted: store.daemon?.adopted,
    hasHealth: !!store.health,
    apiStarted: store.apiStarted,
  })
);

/** S1: compact status chips — tone maps to quiet semantic tint classes. */
const daemonChipTone = computed(() =>
  store.daemon ? (store.daemon.running ? (store.daemon.adopted ? 'info' : 'ok') : 'muted') : 'muted'
);
const healthChipTone = computed(() => (store.healthError ? 'bad' : 'ok'));
const daemonBadgeText = computed(() => {
  if (store.daemon) return store.daemon.running ? (store.daemon.adopted ? 'running (adopted)' : 'running') : 'stopped';
  return store.daemonError === 'bridge-ausente' ? 'bridge missing' : 'no status';
});
const categoryColor = computed(() => 'primary');

function urgencyColor(urgency: string): string {
  if (urgency === 'high') return 'danger';
  if (urgency === 'medium') return 'warning';
  return 'medium';
}

function layerBadgeColor(layer: string): string {
  if (layer === 'rule') return 'primary';
  if (layer === 'llm_router') return 'secondary';
  return 'warning';
}

function telegramBadgeColor(status: string): string {
  if (status.startsWith('error')) return 'danger';
  if (status === 'skipped') return 'medium';
  return 'success';
}

function financeBadgeColor(status: string): string {
  if (status.startsWith('error')) return 'danger';
  if (status.includes('skipped') || status.includes('ignored')) return 'medium';
  return 'success';
}

/** Restart double-arm (S6): click 1 arms the button for 3s ("Confirm restart?",
 * warn tint); expiry disarms silently; only click 2 fires the existing bridge
 * restart. No server-side change — pure client guard. */
const RESTART_ARM_MS = 3000;
const restartArmed = ref(false);
let restartArmTimer: number | null = null;

function clearRestartArmTimer(): void {
  if (restartArmTimer !== null) {
    window.clearTimeout(restartArmTimer);
    restartArmTimer = null;
  }
}

function disarmRestart(): void {
  restartArmed.value = false;
  clearRestartArmTimer();
}

function onRestartDaemon(): void {
  if (!restartArmed.value) {
    restartArmed.value = true;
    clearRestartArmTimer();
    restartArmTimer = window.setTimeout(disarmRestart, RESTART_ARM_MS);
    return;
  }
  disarmRestart();
  void store.restartDaemon().then(() => store.pollDaemon());
}

function retryPolls() {
  void store.loadHealth();
  void store.pollFeed();
}

const feedPoller = usePolling(async () => {
  await store.pollFeed();
  // relative ages recompute on every 2s poll tick via the shared render clock
  nowMs.value = Date.now();
}, { intervalMs: 2000 });
const statusPoller = usePolling(async () => {
  await Promise.all([store.loadHealth(), store.pollDaemon()]);
}, {
  intervalMs: 5000
});

onMounted(() => {
  feedPoller.start();
  statusPoller.start();
});

onUnmounted(() => {
  feedPoller.stop();
  statusPoller.stop();
  // never leave the armed restart timer running past the view lifetime
  disarmRestart();
});
</script>

<style scoped>
/* S2: header end slot keeps the inline badge away from the window edge */
.nb-head-end {
  padding-inline-end: var(--nb-space-4);
}

.nb-cards {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
  gap: var(--nb-space-3);
  margin-bottom: 0;
}
/* S1 (iter3): ONE left-aligned header row — flex-direction: row beats
   Ionic's :host column (which centered the title horizontally and dropped
   the chip below it); title at fs-title 16px/600, chip adjacent with a
   --nb-space-2 gap */
.nb-card-head {
  display: flex;
  flex-direction: row;
  align-items: center;
  gap: var(--nb-space-2);
}
.nb-card-head ion-card-title {
  font-size: var(--nb-fs-title);
  font-weight: 600;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
}

/* Soft status chip + dot + tone classes live in theme/nb-shared.css so the
   Settings connector rows share the same tinted chip system */

/* armed restart: chip-warn tint pair overrides the outline button colors */
.nb-restart-armed {
  --background: var(--nb-chip-warn-bg);
  --border-color: var(--nb-warning);
  --color: var(--nb-chip-warn-fg);
  --box-shadow: none;
}

.nb-kv {
  display: flex;
  justify-content: space-between;
  gap: var(--nb-space-2);
  font-size: var(--nb-fs-secondary);
  padding: var(--nb-space-1) 0;
}
/* S3: last kv row inherits the card-content 12px bottom inset like the footer */
.nb-kv:last-child {
  padding-bottom: 0;
}
/* S5: normalized kv label column so PID/watermark rows align across cards */
.nb-kv span {
  min-width: 160px;
}
.nb-kv span {
  color: var(--nb-text-secondary);
}
.nb-kv strong {
  font-weight: 600;
}
/* R2: value column that stacks the live age under the absolute stamp */
.nb-kv-stack {
  display: flex;
  flex-direction: column;
  align-items: flex-end;
  text-align: right;
  min-width: 0;
}
.nb-age-secondary {
  font-size: var(--nb-fs-micro);
  color: var(--nb-text-muted);
}
/* feed header + its kind chips share ONE row; wraps under the h2 when narrow
   (chips use the shared .nb-filter-chips container from theme/nb-shared.css) */
.nb-feed-header {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: var(--nb-space-2) var(--nb-space-3);
  margin: var(--nb-space-4) 0 var(--nb-space-2);
}
.nb-feed-header h2 {
  margin: 0;
  font-size: var(--nb-fs-title-lg);
  font-weight: 600;
  line-height: 1.3;
}
.nb-feed {
  display: flex;
  flex-direction: column;
  gap: var(--nb-space-2);
}
.nb-event {
  border: 1px solid var(--nb-border-default);
  border-radius: var(--nb-radius-md);
  padding: var(--nb-space-2) var(--nb-space-3);
  background: var(--ion-card-background);
  /* click-to-expand target: pointer + hover tint, collapsed height kept */
  cursor: pointer;
}
.nb-event:hover {
  border-color: var(--nb-border-strong);
}
.nb-event:focus-visible {
  outline: 2px solid var(--nb-info);
  outline-offset: 2px;
}
/* chevron in the meta gutter: rotates 90° when the row is open */
.nb-event-chev {
  flex: none;
  font-size: 12px;
  margin-top: 2px;
  color: var(--nb-text-secondary);
  transition: transform 120ms ease;
}
.nb-event-open .nb-event-chev {
  transform: rotate(90deg);
}
/* expanded raw payload: elevated inset + mono micro + full-payload copy */
.nb-event-raw {
  position: relative;
  margin-top: var(--nb-space-2);
  padding: var(--nb-space-2) var(--nb-space-3);
  border-radius: var(--nb-radius-sm);
  background: var(--nb-bg-elevated);
}
/* the source label ("bot" etc.) as a micro caption, only in the expanded view */
.nb-event-raw-source {
  display: block;
  margin-bottom: var(--nb-space-1);
  font-size: var(--nb-fs-micro);
  color: var(--nb-text-muted);
}
.nb-event-raw-text {
  margin: 0;
  font-family: var(--nb-mono);
  font-size: var(--nb-fs-micro);
  line-height: 1.45;
  white-space: pre-wrap;
  word-break: break-all;
  max-height: 240px;
  overflow-y: auto;
  color: var(--nb-text-primary);
}
.nb-event-raw-note {
  font-size: var(--nb-fs-micro);
  color: var(--nb-text-muted);
}
/* copy affordance is noise at rest — same hover reveal as the log rows */
.nb-raw-copy {
  position: absolute;
  top: var(--nb-space-1);
  right: var(--nb-space-1);
  display: inline-flex;
  align-items: center;
  gap: var(--nb-space-1);
  background: none;
  border: none;
  color: var(--nb-text-secondary);
  cursor: pointer;
  padding: 2px var(--nb-space-1); /* 2px micro-inner exception */
  opacity: 0;
  transition: opacity 120ms ease;
}
.nb-event-raw:hover .nb-raw-copy,
.nb-raw-copy:focus-visible {
  opacity: 1;
}
.nb-raw-copy:hover {
  color: var(--nb-text-primary);
}
.nb-raw-copied {
  font-size: var(--nb-fs-micro);
  color: var(--ion-color-success);
}
.nb-event-startup {
  opacity: 0.65;
}
.nb-event-top {
  display: flex;
  gap: var(--nb-space-2);
  align-items: baseline;
  font-size: var(--nb-fs-micro);
  color: var(--nb-text-secondary);
}
/* kind identity chip: leading tinted icon circle — startup slate (neutral
   elevated), notification teal, finance green; the shared tinted-chip DNA
   (soft ~15% alpha background, icon at full strength) */
.nb-kind {
  flex: none;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 22px;
  height: 22px;
  border-radius: var(--nb-radius-full);
  background: var(--nb-bg-elevated);
  color: var(--nb-text-secondary);
  /* center the circle against the micro text under the row's baseline layout */
  align-self: center;
}
.nb-kind ion-icon {
  font-size: 14px;
}
/* R2: the muted kind tag next to the icon ("startup") — one step quieter
   than the meta row's secondary copy; replaces the redundant bold label */
.nb-kind-caption {
  flex: none;
  color: var(--nb-text-muted);
}
.nb-kind-notification {
  background: var(--nb-teal-muted);
  color: var(--nb-teal-light);
}
.nb-kind-finance {
  background: var(--nb-green-muted);
  color: var(--nb-green);
}
.nb-when,
.nb-recid {
  color: var(--nb-text-secondary);
}
/* the relative caption reads one step quieter than the absolute stamp */
.nb-when-rel {
  color: var(--nb-text-muted);
}
/* R2 finding 4: one-line cue under the filter chips for an all-startup feed */
.nb-feed-hint {
  margin: 0 0 var(--nb-space-2);
  font-size: var(--nb-fs-secondary);
  color: var(--nb-text-muted);
}
.nb-event-summary {
  color: var(--nb-text-secondary);
  font-size: var(--nb-fs-secondary);
  margin-top: var(--nb-space-1);
}
/* S4: chip rhythm — one step between chip rows, one micro-gap inside;
   ion-chip's own 4px host margins are neutralized for uniform gaps */
.nb-chips ion-chip {
  margin: 0;
}
.nb-chips {
  display: flex;
  flex-wrap: wrap;
  gap: var(--nb-space-1);
  margin-top: var(--nb-space-2);
  align-items: center;
}
.nb-badge {
  font-size: var(--nb-fs-micro);
}
.nb-actions {
  margin-top: var(--nb-space-2);
  display: flex;
  flex-wrap: wrap;
  gap: var(--nb-space-1);
}
.nb-action {
  font-family: var(--nb-mono);
  font-size: var(--nb-fs-micro);
  padding: 2px var(--nb-space-1); /* 2px micro-inner exception */
  border-radius: var(--nb-radius-sm);
  background: var(--nb-purple-muted);
}
</style>
