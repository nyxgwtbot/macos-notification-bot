<template>
  <ion-page>
    <ion-header>
      <ion-toolbar class="nb-page-header">
        <!-- R8: the page title is a real heading (level 1) — the shared
             .nb-page-header pattern's a11y contract, kept in every view -->
        <ion-title class="nb-chat-title" role="heading" aria-level="1">
          <span class="nb-title-row">
            <!-- F5: daemon live dot — green running / red stopped / gray unknown,
                 light 5s health poll, click jumps to the Status tab -->
            <button
              type="button"
              class="nb-daemon-dot"
              :class="`nb-dot-${daemonTone}`"
              :title="daemonDotTitle"
              :aria-label="daemonDotTitle"
              @click="goStatus"
            ></button>
            <span>Chat</span>
          </span>
        </ion-title>
        <ion-buttons slot="end" class="nb-header-sessions">
          <ion-button
            v-if="state.artifactUrl && !railOpen"
            size="small"
            fill="outline"
            @click="railOpen = true"
          >
            Show dashboard
          </ion-button>
          <ion-select
            class="nb-session-select"
            interface="popover"
            placeholder="Select session"
            aria-label="Session"
            :value="state.sessionId ?? ''"
            @ionChange="onSessionChange($event)"
          >
            <ion-select-option v-for="session in state.sessions" :key="session.session_id" :value="session.session_id">
              {{ session.preview }} ({{ session.turns }} turns)
            </ion-select-option>
          </ion-select>
          <ion-button size="small" fill="outline" class="nb-btn-quiet" @click="newSession">New chat</ion-button>
        </ion-buttons>
      </ion-toolbar>
    </ion-header>

    <ion-content class="nb-content nb-chat-content">
      <div class="nb-chat-layout" :class="{ 'has-artifact': state.artifactUrl && railOpen }">
        <!-- Main Column: Transcript + Error + Composer -->
        <div class="nb-chat-main">
          <!-- Offline banner: sessions endpoint unreachable -> error strip + retry, never blank -->
          <div v-if="loadError" class="nb-error-strip">
            <div class="nb-error-strip-msg">
              <ion-icon aria-hidden="true" :icon="cloudOfflineOutline" />
              <span>API offline: {{ loadError }}</span>
            </div>
            <ion-button size="small" fill="outline" @click="loadSessions()">Retry</ion-button>
          </div>

          <!-- Transcript bubbles with internal scroll and accessible log role.
               F1: one ordered item list — bubbles interleaved with per-turn
               tool-activity groups (under the turn's user bubble, before its
               reply). -->
          <div ref="transcriptEl" class="nb-transcript" role="log" aria-live="polite">
            <template v-for="item in state.items" :key="item.id">
              <div
                v-if="item.kind === 'bubble'"
                class="nb-bubble"
                :class="item.role === 'user' ? 'nb-bubble-user' : 'nb-bubble-assistant'"
                tabindex="0"
              >
                <span v-if="item.role === 'user'" class="nb-bubble-plain">{{ item.text }}</span>
                <!-- C6: assistant replies render through mdLite (escape-first; uniform
                     .nb-md rhythm). User replies stay verbatim pre-wrap. -->
                <div v-else class="nb-md" v-html="mdLite(item.text)"></div>
                <!-- F2: right-aligned micro timestamp + hover copy affordance
                     (copy the plain bubble TEXT, not the markdown markup) -->
                <div class="nb-bubble-meta">
                  <time class="nb-bubble-time" :datetime="item.ts ?? undefined">{{ formatBubbleTime(item.ts) }}</time>
                  <button
                    type="button"
                    class="nb-bubble-copy"
                    aria-label="Copy message"
                    @click="copyBubble(item)"
                  >
                    <ion-icon aria-hidden="true" :icon="copiedBubbles.has(item.id) ? checkmarkOutline : copyOutline" />
                  </button>
                </div>
              </div>

              <!-- F1: collapsible per-turn tool-activity group (collapsed by
                   default, count label as the toggle) -->
              <div v-else class="nb-turn-tools">
                <button
                  type="button"
                  class="nb-turn-tools-head"
                  :aria-expanded="expandedGroups.has(item.id)"
                  :aria-controls="`nb-tools-${item.id}`"
                  @click="toggleGroup(item.id)"
                >
                  <span class="nb-turn-tools-label">Tool activity · {{ item.chips.length }}</span>
                  <ion-icon aria-hidden="true" :icon="expandedGroups.has(item.id) ? chevronUpOutline : chevronDownOutline" />
                </button>
                <div v-show="expandedGroups.has(item.id)" :id="`nb-tools-${item.id}`" class="nb-tool-activity">
                  <div v-for="chip in item.chips" :key="chip.id" class="nb-tool-chip" :class="{ 'nb-tool-chip-error': !chip.ok }">
                    <button
                      type="button"
                      class="nb-tool-chip-head"
                      :aria-expanded="expandedChips.has(chip.id)"
                      @click="toggleChip(chip.id)"
                    >
                      <span class="nb-tool-tool">{{ chip.tool }}</span>
                      <ion-badge :color="chip.ok ? 'success' : 'danger'">{{ chip.ok ? 'ok' : 'error' }}</ion-badge>
                    </button>
                    <pre v-if="expandedChips.has(chip.id)" class="nb-tool-preview">{{ chip.preview }}</pre>
                  </div>
                </div>
              </div>
            </template>

            <!-- Onboarding empty state when no conversation is active -->
            <div v-if="state.items.length === 0 && !state.pending" class="nb-onboarding">
              <h2 class="nb-onboarding-title">What should I build?</h2>
              <p class="nb-onboarding-desc">
                Ask a question and the agent runs local tools, may request macOS consent for native actions, and renders dashboards right next to this conversation.
              </p>
              <div class="nb-onboarding-suggestions">
                <button
                  v-for="suggestion in SUGGESTIONS"
                  :key="suggestion.text"
                  type="button"
                  class="nb-suggestion-card"
                  @click="prefill(suggestion.text)"
                >
                  <ion-icon aria-hidden="true" :icon="suggestion.icon" />
                  <span class="nb-suggestion-copy">
                    <span class="nb-suggestion-title">{{ suggestion.title }}</span>
                    <span class="nb-suggestion-hint">{{ suggestion.hint }}</span>
                  </span>
                </button>
              </div>
              <p class="nb-onboarding-tags">Dashboards · Test notifications · Event summaries · Reminders · Notes · System controls</p>
            </div>

            <!-- F6: pending bubble with a CSS-only animated dot pattern -->
            <div v-if="state.pending" class="nb-bubble nb-bubble-assistant nb-pending-bubble">
              <span class="nb-typing" aria-hidden="true"><i></i><i></i><i></i></span>
              <span>Working…</span>
            </div>
          </div>

          <!-- Turn error banner with retry -->
          <div v-if="state.error" class="nb-error-strip">
            <div class="nb-error-strip-msg">
              <ion-icon aria-hidden="true" :icon="alertCircleOutline" />
              <span>Turn failed: {{ state.error }}</span>
            </div>
            <ion-button size="small" fill="outline" @click="retryLast">Retry</ion-button>
          </div>

          <!-- Composer: bordered group (C3) — textarea + hint/send foot inside one surface -->
          <div class="nb-composer">
            <div class="nb-composer-surface">
              <ion-textarea
                ref="composerEl"
                class="nb-composer-input"
                placeholder="Message the bot…"
                :value="state.draft"
                :auto-grow="true"
                :rows="1"
                @ionInput="onDraftInput($event as IonValueEvent)"
                @keydown="onComposerKeydown($event)"
              />
              <div class="nb-composer-foot">
                <span class="nb-composer-hint">⏎ Send · ⇧⏎ Newline</span>
                <ion-button class="nb-send-btn" :disabled="!canSend" fill="solid" @click="send">
                  <ion-spinner v-if="state.pending" name="crescent" />
                  <template v-else>Send</template>
                </ion-button>
              </div>
            </div>
          </div>
        </div>

        <!-- Right Rail: Artifact Canvas (Dashboard iframe) -->
        <div v-if="state.artifactUrl && railOpen" class="nb-chat-rail">
          <ion-card class="nb-artifact">
            <ion-card-header class="nb-artifact-header">
              <ion-card-title class="nb-artifact-title">Dashboard</ion-card-title>
              <div class="nb-artifact-actions">
                <ion-button size="small" fill="clear" aria-label="Reload dashboard" @click="reloadArtifact">
                  <ion-icon slot="icon-only" aria-hidden="true" :icon="refreshOutline" />
                </ion-button>
                <ion-button size="small" fill="clear" aria-label="Copy dashboard link" @click="copyArtifactLink">
                  <ion-icon slot="icon-only" aria-hidden="true" :icon="copiedRecently ? checkmarkOutline : copyOutline" />
                </ion-button>
                <ion-button size="small" fill="clear" aria-label="Collapse rail" @click="railOpen = false">
                  <ion-icon slot="icon-only" aria-hidden="true" :icon="closeOutline" />
                </ion-button>
              </div>
            </ion-card-header>
            <ion-card-content class="nb-artifact-content">
              <!-- sandbox="" strictly preserved: no script execution within the dashboard frame -->
              <iframe
                :key="reloadKey"
                class="nb-artifact-frame"
                sandbox=""
                :src="state.artifactUrl"
                title="Generated dashboard"
              ></iframe>
            </ion-card-content>
          </ion-card>
        </div>
      </div>
    </ion-content>
  </ion-page>
</template>

<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, reactive, ref, watch } from 'vue';
import { useRouter } from 'vue-router';
import {
  IonBadge,
  IonButton,
  IonButtons,
  IonCard,
  IonCardContent,
  IonCardHeader,
  IonCardTitle,
  IonContent,
  IonHeader,
  IonIcon,
  IonPage,
  IonSelect,
  IonSelectOption,
  IonSpinner,
  IonTextarea,
  IonTitle,
  IonToolbar
} from '@ionic/vue';
import {
  alertCircleOutline,
  barChartOutline,
  beakerOutline,
  checkmarkOutline,
  chevronDownOutline,
  chevronUpOutline,
  closeOutline,
  cloudOfflineOutline,
  copyOutline,
  listOutline,
  refreshOutline
} from 'ionicons/icons';

import { notifyClient, errorText } from '@/services/notifyClient';
import { usePolling } from '@/composables/usePolling';
import {
  formatBubbleTime,
  initialChatState,
  isIpcTimeout,
  reduceChat,
  type ChatBubble,
  type ChatEvent,
  type ChatState
} from '@/viewModels/chatViewModel';
import { mdLite } from '@/utils/mdLite';

/** F3: card-like onboarding suggestions — click prefills the composer draft
 * (no auto-send); text is the prompt the agent performs best. */
const SUGGESTIONS = [
  {
    icon: barChartOutline,
    text: 'Draft a spending dashboard for this month',
    title: 'Draft a spending dashboard',
    hint: 'Turns this month’s finance ledger into a live dashboard card.'
  },
  {
    icon: beakerOutline,
    text: 'Send a test notification',
    title: 'Send a test notification',
    hint: 'Fire sample macOS notifications to verify the flow end to end.'
  },
  {
    icon: listOutline,
    text: 'Summarize this week\'s event log',
    title: 'Summarize this week\'s event log',
    hint: 'A plain-language digest of the last 7 days of events.'
  }
] as const;

const state = ref<ChatState>(initialChatState());
const loadError = ref<string | null>(null);
const lastMessage = ref<string | null>(null);
const expandedChips = reactive(new Set<string>());
/** F1: per-turn tool-activity groups — collapsed by default; the turn's
 * count label toggles it. Expanded state is component-only memory. */
const expandedGroups = reactive(new Set<string>());
/** F2: transient per-bubble "copied" state (icon swaps to a checkmark). */
const copiedBubbles = reactive(new Set<string>());
const railOpen = ref(true);
const reloadKey = ref(0);
const copiedRecently = ref(false);
const transcriptEl = ref<HTMLElement | null>(null);
const composerEl = ref<{ $el?: HTMLElement } | null>(null);

const router = useRouter();

const canSend = computed(() => !state.value.pending && state.value.draft.trim() !== '');

function dispatch(event: ChatEvent): void {
  state.value = reduceChat(state.value, event);
}

// Watch state.artifactUrl and force railOpen = true on arrival so a new dashboard is never missed
watch(
  () => state.value.artifactUrl,
  (newUrl) => {
    if (newUrl) {
      railOpen.value = true;
    }
  }
);

// Auto-scroll transcript internally when new items arrive or pending state toggles
watch(
  [() => state.value.items.length, () => state.value.pending, () => state.value.sessionId],
  async () => {
    await nextTick();
    if (transcriptEl.value) {
      transcriptEl.value.scrollTo({ top: transcriptEl.value.scrollHeight, behavior: 'smooth' });
    }
  }
);

async function loadSessions(quietAutoRetry = false): Promise<void> {
  const isQuiet = quietAutoRetry === true;
  const delays = isQuiet ? [1500, 3000, 6000] : [];
  for (let attempt = 0; attempt <= delays.length; attempt++) {
    loadError.value = null;
    dispatch({ type: 'error-cleared' });
    try {
      const { sessions } = await notifyClient.getAgentSessions();
      dispatch({ type: 'sessions-loaded', sessions });
      return;
    } catch (err) {
      if (attempt < delays.length) {
        const delayMs = delays[attempt];
        console.info(`[ChatView] Session load attempt ${attempt + 1} failed (${errorText(err)}). Retrying in ${delayMs}ms...`);
        await new Promise((resolve) => setTimeout(resolve, delayMs));
      } else {
        if (isQuiet) {
          console.warn(`[ChatView] Session load auto-retry exhausted (${errorText(err)}). Surfacing error.`);
        }
        loadError.value = errorText(err);
      }
    }
  }
}

function onSessionChange(event: CustomEvent): void {
  const sessionId = String(event.detail.value || '');
  const nextId = sessionId === '' ? null : sessionId;
  if (nextId === state.value.sessionId) return;
  dispatch({ type: 'session-selected', sessionId: nextId });
  if (nextId !== null) {
    void loadSession(nextId);
  }
}

async function loadSession(sessionId: string): Promise<void> {
  try {
    const detail = await notifyClient.getAgentSession(sessionId);
    dispatch({ type: 'session-loaded', sessionId, records: detail.records });
  } catch (err) {
    dispatch({ type: 'turn-failed', error: errorText(err) });
  }
}

function newSession(): void {
  // null = brand-new conversation server-side on the next send
  dispatch({ type: 'session-selected', sessionId: null });
}

function toggleChip(chipId: string): void {
  if (expandedChips.has(chipId)) {
    expandedChips.delete(chipId);
  } else {
    expandedChips.add(chipId);
  }
}

function toggleGroup(groupId: string): void {
  if (expandedGroups.has(groupId)) {
    expandedGroups.delete(groupId);
  } else {
    expandedGroups.add(groupId);
  }
}

/** F3: prefill (not send) the composer draft from a suggestion and focus it. */
function prefill(text: string): void {
  dispatch({ type: 'prefill', text });
  void focusComposer();
}

async function focusComposer(): Promise<void> {
  await nextTick();
  const host = composerEl.value?.$el as (HTMLElement & { setFocus?: (opts?: { focusDelay?: number }) => void }) | undefined;
  if (host && typeof host.setFocus === 'function') {
    host.setFocus();
  }
}

/** Composer keystrokes keep the VM draft in sync (send-on-enter stays view-side). */
type IonValueEvent = CustomEvent<{ value?: string | null }>;

function onDraftInput(event: IonValueEvent): void {
  dispatch({ type: 'draft-changed', text: String(event.detail?.value ?? '') });
}

function onComposerKeydown(event: KeyboardEvent): void {
  if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    void send();
  }
}

function reloadArtifact(): void {
  reloadKey.value++;
}

async function copyArtifactLink(): Promise<void> {
  if (!state.value.artifactUrl) return;
  try {
    await navigator.clipboard.writeText(state.value.artifactUrl);
    copiedRecently.value = true;
    setTimeout(() => {
      copiedRecently.value = false;
    }, 2000);
  } catch {
    // clipboard write failure swallowed
  }
}

/** F2: copy the bubble's plain text (not the rendered markdown markup). */
async function copyBubble(bubble: ChatBubble): Promise<void> {
  try {
    await navigator.clipboard.writeText(bubble.text);
    copiedBubbles.add(bubble.id);
    setTimeout(() => {
      copiedBubbles.delete(bubble.id);
    }, 2000);
  } catch {
    // clipboard write failure swallowed
  }
}

async function send(): Promise<void> {
  const message = state.value.draft.trim();
  if (!message || state.value.pending) return;
  lastMessage.value = message;
  dispatch({ type: 'send', message });
  try {
    const response = await notifyClient.postAgentTurn(state.value.sessionId, message);
    dispatch({ type: 'turn-succeeded', response });
    void loadSessions();
  } catch (err) {
    dispatch({ type: 'turn-failed', error: errorText(err) });
    // IPC proxy timeout recovery: the python handler thread may still have
    // finished the turn — the session jsonl is the source of truth.
    if (isIpcTimeout(err) && state.value.sessionId) {
      try {
        const detail = await notifyClient.getAgentSession(state.value.sessionId);
        dispatch({ type: 'session-loaded', sessionId: state.value.sessionId, records: detail.records });
      } catch {
        // keep the error line; the user can retry
      }
    }
  }
}

function retryLast(): void {
  if (lastMessage.value === null) return;
  loadError.value = null;
  dispatch({ type: 'error-cleared' });
  prefill(lastMessage.value);
}

/** F5: light 5s health poll — the chat flow itself polls turns only, so a
 * tiny local poll (usePolling) feeds the header dot without coupling to the
 * Status view's pollers. */
async function pollHealthTick(): Promise<void> {
  try {
    const health = await notifyClient.getHealth();
    daemonTone.value = health && health.ok ? 'running' : 'stopped';
  } catch {
    daemonTone.value = 'stopped';
  }
}

const daemonPoller = usePolling(pollHealthTick, { intervalMs: 5000 });

const daemonTone = ref<'running' | 'stopped' | 'unknown'>('unknown');

const daemonDotTitle = computed(() =>
  daemonTone.value === 'running'
    ? 'Notification daemon running'
    : daemonTone.value === 'stopped'
      ? 'Notification daemon stopped'
      : 'Notification daemon status unknown'
);

function goStatus(): void {
  void router.push('/dashboard');
}

onMounted(() => {
  void loadSessions(true);
  daemonPoller.start();
});

onUnmounted(() => {
  daemonPoller.stop();
});
</script>

<style scoped>
.nb-chat-content {
  --overflow: hidden;
  --padding-top: var(--nb-space-4);
  --padding-start: var(--nb-space-4);
  --padding-end: var(--nb-space-4);
  --padding-bottom: var(--nb-space-4);
}

.nb-header-sessions {
  display: flex;
  align-items: center;
  gap: var(--nb-space-2);
  margin-inline-end: var(--nb-space-2);
}

/* F5: title row — live daemon dot sits before the "Chat" label, dot tone
   classes color the state (green/red/gray) */
.nb-chat-title .nb-title-row {
  display: inline-flex;
  align-items: center;
  gap: var(--nb-space-2);
}

.nb-daemon-dot {
  width: 9px;
  height: 9px;
  flex: none;
  padding: 0;
  border: none;
  border-radius: var(--nb-radius-full);
  cursor: pointer;
  background: var(--nb-text-muted);
}

.nb-dot-running {
  background: var(--nb-green);
}

.nb-dot-stopped {
  background: var(--nb-danger);
}

.nb-dot-unknown {
  background: var(--nb-text-muted);
}

/* C1: quiet secondary header button — must not out-shout the primary flow.
   md outline buttons in a toolbar read var(--ion-toolbar-color) for text AND
   border (that's the loud white outline) — re-point that custom property to
   the secondary text tone. Background/radius need outer !important because
   Chromium lets :host() declarations beat equal-specificity outer rules. */
.nb-btn-quiet {
  --background: var(--nb-bg-elevated) !important;
  --background-hover: var(--nb-bg-hover) !important;
  --background-activated: var(--nb-bg-hover) !important;
  --background-focused: var(--nb-bg-hover) !important;
  --background-focused-opacity: 1 !important;
  --ion-toolbar-color: var(--nb-text-secondary);
  --color: var(--nb-text-secondary) !important;
  --color-hover: var(--nb-text-primary) !important;
  --color-activated: var(--nb-text-primary) !important;
  --color-focused: var(--nb-text-primary) !important;
  --border-radius: var(--nb-radius-full) !important;
  --padding-start: var(--nb-space-3) !important;
  --padding-end: var(--nb-space-3) !important;
  font-size: var(--nb-fs-secondary) !important;
  white-space: nowrap !important;
  flex-shrink: 0;
  margin: 0;
}

/* C4: one shared control height for the header end-slot (select + buttons
   align on the same row/baseline regardless of Ionic's in-toolbar sizing). */
.nb-header-sessions ion-button,
.nb-header-sessions .nb-session-select {
  min-height: 28px !important;
  height: 28px !important;
  margin: 0;
}

.nb-header-sessions ion-button {
  flex-shrink: 0;
  white-space: nowrap !important;
  font-size: var(--nb-fs-secondary) !important;
  --padding-start: var(--nb-space-3) !important;
  --padding-end: var(--nb-space-3) !important;
}

.nb-session-select {
  flex: 0 1 auto;
  /* C4: cap the pill; long session labels truncate instead of stretching the header */
  max-width: 360px;
  min-height: 28px; /* shared end-slot height (see .nb-header-sessions rule) */
  border-radius: var(--nb-radius-full);
  background: var(--nb-bg-elevated);
  border: 1px solid var(--nb-border-default);
  padding: 0 var(--nb-space-3);
  font-size: var(--nb-fs-secondary);
}

.nb-chat-layout {
  display: grid;
  grid-template-columns: minmax(0, 1fr);
  height: 100%;
  min-height: 0;
}

.nb-chat-layout.has-artifact {
  grid-template-columns: minmax(360px, 440px) minmax(0, 1fr);
  gap: var(--nb-space-4);
}

.nb-chat-main {
  display: flex;
  flex-direction: column;
  height: 100%;
  min-height: 0;
}

.nb-error-strip-msg {
  display: flex;
  align-items: center;
  gap: var(--nb-space-2);
}

.nb-transcript {
  flex: 1 1 0;
  min-height: 0;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: var(--nb-space-2);
  /* C5: top breathing under the header — first bubble never sits flush */
  padding-top: var(--nb-space-3);
}

.nb-bubble {
  max-width: 82%;
  padding: var(--nb-space-2) var(--nb-space-3);
  border-radius: var(--nb-radius-lg);
  font-size: var(--nb-fs-body);
  line-height: 1.5;
  white-space: pre-wrap;
  word-break: break-word;
}

.nb-bubble-user {
  align-self: flex-end;
  background: var(--nb-teal-muted);
  border: 1px solid var(--nb-teal-light);
}

.nb-bubble-assistant {
  align-self: flex-start;
  background: var(--ion-card-background);
  border: 1px solid var(--nb-border-default);
  /* C6: assistant replies are structured markdown now — no pre-wrap rhythm */
  white-space: normal;
}

/* C6: uniform block margins inside markdown (p/h/li share one step; the
   bubble's own padding frames the content). Headings stay at body size.
   All md rules use :deep() — the markup is produced by v-html and carries
   no scoped data attribute of its own. */
.nb-bubble-plain {
  display: inline;
}
.nb-bubble-assistant :deep(.nb-md :is(p, h1, h2, h3, h4, h5, h6, ul, ol, li)) {
  margin: var(--nb-space-2) 0;
}
.nb-bubble-assistant :deep(.nb-md > :first-child) {
  margin-top: 0;
}
.nb-bubble-assistant :deep(.nb-md > :last-child) {
  margin-bottom: 0;
}
.nb-bubble-assistant :deep(.nb-md h1),
.nb-bubble-assistant :deep(.nb-md h2),
.nb-bubble-assistant :deep(.nb-md h3),
.nb-bubble-assistant :deep(.nb-md h4),
.nb-bubble-assistant :deep(.nb-md h5),
.nb-bubble-assistant :deep(.nb-md h6) {
  font-size: var(--nb-fs-body);
  font-weight: 600;
  line-height: 1.35;
  color: var(--nb-text-primary);
}
/* list markers aligned with the text column */
.nb-bubble-assistant :deep(.nb-md ul),
.nb-bubble-assistant :deep(.nb-md ol) {
  padding-inline-start: 1.25em;
}
.nb-bubble-assistant :deep(.nb-md code) {
  font-family: var(--nb-mono);
  font-size: var(--nb-fs-secondary);
  background: var(--nb-bg-elevated);
  padding: 0 var(--nb-space-1);
  border-radius: var(--nb-radius-sm);
}

/* F2: bubble meta row — right-aligned micro timestamp + hover copy icon
   (opacity 0→1 on hover and on keyboard focus-within) */
.nb-bubble-meta {
  display: flex;
  justify-content: flex-end;
  align-items: center;
  gap: var(--nb-space-1);
  margin-top: 2px; /* 2px micro-inner exception (chip padding precedent) */
}

.nb-bubble-time {
  font-size: var(--nb-fs-micro);
  line-height: 1.45;
  color: var(--nb-text-muted);
}

.nb-bubble-copy {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 18px;
  height: 18px;
  padding: 0;
  background: transparent;
  border: none;
  border-radius: var(--nb-radius-sm);
  color: var(--nb-text-secondary);
  font-size: 13px;
  cursor: pointer;
  opacity: 0;
  transition: opacity 120ms ease;
}

.nb-bubble:hover .nb-bubble-copy,
.nb-bubble:focus-within .nb-bubble-copy,
.nb-bubble-copy:focus-visible {
  opacity: 1;
}

.nb-bubble-copy:hover {
  color: var(--nb-text-primary);
}

/* F1: per-turn tool-activity group (assistant-side region) */
.nb-turn-tools {
  align-self: flex-start;
  max-width: 82%;
  display: flex;
  flex-direction: column;
  gap: var(--nb-space-1);
}

.nb-turn-tools-head {
  display: inline-flex;
  align-items: center;
  gap: var(--nb-space-1);
  width: max-content;
  margin: 0;
  padding: 0;
  background: transparent;
  border: none;
  cursor: pointer;
  color: var(--nb-text-muted);
}

.nb-turn-tools-label {
  font-size: var(--nb-fs-micro);
}

.nb-turn-tools-head ion-icon {
  font-size: 12px;
  color: var(--nb-text-muted);
}

/* F6: CSS-only typing dots for the pending bubble */
.nb-pending-bubble {
  display: flex;
  align-items: center;
  gap: var(--nb-space-2);
  font-size: var(--nb-fs-secondary);
  color: var(--nb-text-secondary);
}

.nb-typing {
  display: inline-flex;
  align-items: center;
  gap: 3px;
}

.nb-typing i {
  width: 6px;
  height: 6px;
  border-radius: var(--nb-radius-full);
  background: var(--nb-text-secondary);
  animation: nb-typing-pulse 1.2s ease-in-out infinite;
}

.nb-typing i:nth-child(2) {
  animation-delay: 150ms;
}

.nb-typing i:nth-child(3) {
  animation-delay: 300ms;
}

@keyframes nb-typing-pulse {
  0%,
  60%,
  100% {
    transform: translateY(0);
    opacity: 0.35;
  }
  30% {
    transform: translateY(-3px);
    opacity: 1;
  }
}

.nb-onboarding {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  text-align: center;
  margin: auto 0;
  padding: var(--nb-space-6) var(--nb-space-4);
  max-width: 520px;
  align-self: center;
}

.nb-onboarding-title {
  font-size: var(--nb-fs-title-lg);
  font-weight: 600;
  line-height: 1.3;
  margin: 0 0 var(--nb-space-2);
  color: var(--nb-text-primary);
}

.nb-onboarding-desc {
  font-size: var(--nb-fs-secondary);
  color: var(--nb-text-secondary);
  line-height: 1.45;
  margin: 0 0 var(--nb-space-4);
}

.nb-onboarding-suggestions {
  display: flex;
  flex-direction: column;
  gap: var(--nb-space-2);
  width: 100%;
}

/* F3: card-like suggestions — icon left, title + one-line hint right */
.nb-suggestion-card {
  display: flex;
  align-items: flex-start;
  gap: var(--nb-space-3);
  width: 100%;
  text-align: left;
  background: var(--ion-card-background);
  border: 1px solid var(--nb-border-default);
  border-radius: var(--nb-radius-md);
  padding: var(--nb-space-3);
  color: var(--nb-text-primary);
  font: inherit;
  cursor: pointer;
  transition: background 120ms ease, border-color 120ms ease;
}

.nb-suggestion-card:hover {
  background: var(--nb-bg-hover);
  border-color: var(--nb-border-strong);
}

.nb-suggestion-card:focus-visible {
  outline: 2px solid var(--nb-teal);
  outline-offset: 2px;
}

.nb-suggestion-card ion-icon {
  flex: none;
  font-size: 20px;
  margin-top: 1px;
  color: var(--nb-teal-light);
}

.nb-suggestion-copy {
  display: flex;
  flex-direction: column;
  gap: 2px; /* title/hint micro-gap */
  min-width: 0;
}

.nb-suggestion-title {
  font-size: var(--nb-fs-secondary);
  font-weight: 600;
}

.nb-suggestion-hint {
  font-size: var(--nb-fs-micro);
  color: var(--nb-text-secondary);
  line-height: 1.45;
}

.nb-onboarding-tags {
  margin: var(--nb-space-3) 0 0;
  font-size: var(--nb-fs-micro);
  color: var(--nb-text-muted);
}

/* compact inline chips (C2) — one wrapping flex row, tight gap */
.nb-tool-activity {
  display: flex;
  flex-direction: row;
  flex-wrap: wrap;
  align-items: flex-start;
  gap: var(--nb-space-1);
  margin: 0;
  flex-shrink: 0;
}

.nb-tool-chip {
  display: inline-flex;
  flex-direction: column;
  flex: 0 1 auto;
  max-width: 100%;
  border: 1px solid var(--nb-border-default);
  border-radius: var(--nb-radius-md);
  background: var(--ion-card-background);
}

.nb-tool-chip-error {
  border-color: var(--ion-color-danger);
}

/* badge sits adjacent to the label */
.nb-tool-chip-head {
  display: inline-flex;
  align-items: center;
  justify-content: flex-start;
  gap: var(--nb-space-2);
  padding: var(--nb-space-1) var(--nb-space-2);
  background: transparent;
  border: none;
  cursor: pointer;
  color: inherit;
  font: inherit;
  white-space: nowrap;
}

.nb-tool-tool {
  font-family: var(--nb-mono);
  font-size: var(--nb-fs-secondary);
}

.nb-tool-preview {
  margin: 0;
  width: max-content;
  max-width: min(420px, 100%);
  max-height: 220px;
  overflow-y: auto;
  overflow-x: auto;
  padding: 0 var(--nb-space-2) var(--nb-space-2);
  font-family: var(--nb-mono);
  font-size: var(--nb-fs-micro);
  line-height: 1.45;
  color: var(--nb-text-secondary);
}

/* C3: one bordered composer group — textarea + hint/send foot inside it */
.nb-composer {
  flex-shrink: 0;
  margin-top: var(--nb-space-2);
}

.nb-composer-surface {
  display: flex;
  flex-direction: column;
  border: 1px solid var(--nb-border-strong);
  border-radius: var(--nb-radius-md);
  background: var(--nb-bg-elevated);
  padding: var(--nb-space-2) var(--nb-space-2) var(--nb-space-1);
  transition: border-color 120ms ease, box-shadow 120ms ease;
}

/* F4b: focus-visible ring on the container — border-strong + subtle outer glow */
.nb-composer-surface:focus-within {
  border-color: var(--nb-border-strong);
  box-shadow: 0 0 0 3px var(--nb-teal-muted);
}

.nb-composer-input {
  --background: transparent;
  --padding-start: var(--nb-space-2);
  --padding-end: var(--nb-space-2);
  --padding-top: 0;
  --padding-bottom: 0;
  font-size: var(--nb-fs-body);
}

/* F4a: keyboard hint bottom-left + Send bottom-right inside one foot row */
.nb-composer-foot {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: var(--nb-space-2);
}

.nb-composer-hint {
  padding-inline-start: var(--nb-space-2);
  font-size: var(--nb-fs-micro);
  color: var(--nb-text-muted);
}

/* F4c: Send brightness — 40% until the draft is non-empty */
.nb-composer-foot ion-button[disabled] {
  opacity: 0.4;
}

.nb-chat-rail {
  min-width: 0;
  height: 100%;
  display: flex;
  flex-direction: column;
}

.nb-artifact {
  display: flex;
  flex-direction: column;
  height: 100%;
  margin: 0;
  border: 1px solid var(--nb-border-default);
  border-radius: var(--nb-radius-md);
  background: var(--ion-card-background);
  overflow: hidden;
}

.nb-artifact-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: var(--nb-space-2) var(--nb-space-3);
  border-bottom: 1px solid var(--nb-border-default);
  flex-shrink: 0;
}

.nb-artifact-title {
  font-size: var(--nb-fs-title);
  font-weight: 600;
  line-height: 1.3;
}

.nb-artifact-actions {
  display: flex;
  align-items: center;
  gap: var(--nb-space-1);
}

.nb-artifact-content {
  flex: 1 1 0;
  min-height: 0;
  padding: 0;
  display: flex;
}

.nb-artifact-frame {
  width: 100%;
  height: 100%;
  border: none;
  background: var(--nb-artifact-bg, #ffffff);
}
</style>