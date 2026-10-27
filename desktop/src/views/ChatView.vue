<template>
  <ion-page>
    <ion-header>
      <ion-toolbar class="nb-page-header">
        <ion-title>Chat</ion-title>
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
        <!-- Main Column: Transcript + Tools + Error + Composer -->
        <div class="nb-chat-main">
          <!-- Offline banner: sessions endpoint unreachable -> error strip + retry, never blank -->
          <div v-if="loadError" class="nb-error-strip">
            <div class="nb-error-strip-msg">
              <ion-icon aria-hidden="true" :icon="cloudOfflineOutline" />
              <span>API offline: {{ loadError }}</span>
            </div>
            <ion-button size="small" fill="outline" @click="loadSessions">Retry</ion-button>
          </div>

          <!-- Transcript bubbles with internal scroll and accessible log role -->
          <div ref="transcriptEl" class="nb-transcript" role="log" aria-live="polite">
            <div
              v-for="bubble in state.messages"
              :key="bubble.id"
              class="nb-bubble"
              :class="bubble.role === 'user' ? 'nb-bubble-user' : 'nb-bubble-assistant'"
            >
              <span v-if="bubble.role === 'user'" class="nb-bubble-plain">{{ bubble.text }}</span>
              <!-- C6: assistant replies render through mdLite (escape-first; uniform
                   .nb-md rhythm). User replies stay verbatim pre-wrap. -->
              <div v-else class="nb-md" v-html="mdLite(bubble.text)"></div>
            </div>

            <!-- Onboarding empty state when no conversation is active -->
            <div v-if="state.messages.length === 0 && !state.pending" class="nb-onboarding">
              <h2 class="nb-onboarding-title">What should I build?</h2>
              <p class="nb-onboarding-desc">
                Ask a question and the agent runs local tools, may request macOS consent for native actions, and renders dashboards right next to this conversation.
              </p>
              <div class="nb-onboarding-suggestions">
                <ion-chip :outline="true" class="nb-suggestion-chip" @click="suggest('Draft a spending dashboard for this month')">
                  Draft a spending dashboard for this month
                </ion-chip>
                <ion-chip :outline="true" class="nb-suggestion-chip" @click="suggest('Send a test notification')">
                  Send a test notification
                </ion-chip>
                <ion-chip :outline="true" class="nb-suggestion-chip" @click="suggest('Summarize this week\'s event log')">
                  Summarize this week's event log
                </ion-chip>
              </div>
            </div>

            <!-- Pending bubble: assistant thinking indicator with spinner -->
            <div v-if="state.pending" class="nb-bubble nb-bubble-assistant nb-pending-bubble">
              <ion-spinner name="crescent" />
              <span>Working…</span>
            </div>
          </div>

          <!-- Tool activity: collapsible section above the composer (C7) with
               compact inline chips (C2), count label and per-session open state -->
          <div v-if="state.chips.length > 0" class="nb-tool-section">
            <button
              type="button"
              class="nb-tool-section-head"
              :aria-expanded="toolsOpen"
              aria-controls="nb-tool-chips"
              @click="toggleTools"
            >
              <span class="nb-tool-section-label">Tool activity · {{ state.chips.length }}</span>
              <ion-icon aria-hidden="true" :icon="toolsOpen ? chevronUpOutline : chevronDownOutline" />
            </button>
            <div v-show="toolsOpen" id="nb-tool-chips" class="nb-tool-activity">
              <div v-for="chip in state.chips" :key="chip.id" class="nb-tool-chip" :class="{ 'nb-tool-chip-error': !chip.ok }">
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

          <!-- Turn error banner with retry -->
          <div v-if="state.error" class="nb-error-strip">
            <div class="nb-error-strip-msg">
              <ion-icon aria-hidden="true" :icon="alertCircleOutline" />
              <span>Turn failed: {{ state.error }}</span>
            </div>
            <ion-button size="small" fill="outline" @click="retryLast">Retry</ion-button>
          </div>

          <!-- Composer: bordered group (C3) — textarea + Send bottom-right inside one surface -->
          <div class="nb-composer">
            <div class="nb-composer-surface">
              <ion-textarea
                v-model="draft"
                class="nb-composer-input"
                placeholder="Message the bot…"
                :auto-grow="true"
                :rows="1"
                @keydown="onComposerKeydown($event)"
              />
              <div class="nb-composer-send">
                <ion-button :disabled="!canSend" fill="solid" @click="send">
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
import { computed, nextTick, onMounted, reactive, ref, watch } from 'vue';
import {
  IonBadge,
  IonButton,
  IonButtons,
  IonCard,
  IonCardContent,
  IonCardHeader,
  IonCardTitle,
  IonChip,
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
  checkmarkOutline,
  chevronDownOutline,
  chevronUpOutline,
  closeOutline,
  cloudOfflineOutline,
  copyOutline,
  refreshOutline
} from 'ionicons/icons';

import { notifyClient, errorText } from '@/services/notifyClient';
import {
  initialChatState,
  isIpcTimeout,
  reduceChat,
  type ChatEvent,
  type ChatState
} from '@/viewModels/chatViewModel';
import { mdLite } from '@/utils/mdLite';

const state = ref<ChatState>(initialChatState());
const draft = ref('');
const loadError = ref<string | null>(null);
const lastMessage = ref<string | null>(null);
const expandedChips = reactive(new Set<string>());
/** C7: tool-activity panel open state per session (component-only memory). */
const toolsOpenBySession = reactive(new Map<string | null, boolean>());
/** Default: visible inline while the list is small, collapsed past this size. */
const TOOL_PANEL_INLINE_MAX = 6;
const railOpen = ref(true);
const reloadKey = ref(0);
const copiedRecently = ref(false);
const transcriptEl = ref<HTMLElement | null>(null);

const canSend = computed(() => !state.value.pending && draft.value.trim() !== '');

/** C7: open state for the current session — manual toggle wins, otherwise
 * "closed by default only past the inline cap". */
const toolsOpen = computed(() => {
  const manual = toolsOpenBySession.get(state.value.sessionId);
  if (manual !== undefined) return manual;
  return state.value.chips.length <= TOOL_PANEL_INLINE_MAX;
});

function toggleTools(): void {
  toolsOpenBySession.set(state.value.sessionId, !toolsOpen.value);
}

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

// Auto-scroll transcript internally when new messages arrive or pending state toggles
watch(
  [() => state.value.messages.length, () => state.value.pending, () => state.value.sessionId],
  async () => {
    await nextTick();
    if (transcriptEl.value) {
      transcriptEl.value.scrollTo({ top: transcriptEl.value.scrollHeight, behavior: 'smooth' });
    }
  }
);

async function loadSessions(): Promise<void> {
  loadError.value = null;
  try {
    const { sessions } = await notifyClient.getAgentSessions();
    dispatch({ type: 'sessions-loaded', sessions });
  } catch (err) {
    loadError.value = errorText(err);
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

function suggest(text: string): void {
  draft.value = text;
  void send();
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

async function send(): Promise<void> {
  const message = draft.value.trim();
  if (!message || state.value.pending) return;
  draft.value = '';
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
  draft.value = lastMessage.value;
  dispatch({ type: 'error-cleared' });
}

onMounted(loadSessions);
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
}

/* C4: one shared control height for the header end-slot (select + buttons
   align on the same row/baseline regardless of Ionic's in-toolbar sizing). */
.nb-header-sessions ion-button,
.nb-header-sessions .nb-session-select {
  min-height: 28px !important;
  height: 28px !important;
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

.nb-pending-bubble {
  display: flex;
  align-items: center;
  gap: var(--nb-space-2);
  font-size: var(--nb-fs-secondary);
  color: var(--nb-text-secondary);
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

.nb-suggestion-chip {
  justify-content: center;
  cursor: pointer;
  border-color: var(--nb-border-default);
  font-size: var(--nb-fs-secondary);
  color: var(--nb-text-primary);
  margin: 0;
  padding: var(--nb-space-2) var(--nb-space-3);
  border-radius: var(--nb-radius-md);
}

.nb-suggestion-chip:hover {
  background: var(--nb-bg-hover);
  border-color: var(--nb-teal);
}

/* C7: collapsible tool-activity section — hairline divider above the composer */
.nb-tool-section {
  border-top: 1px solid var(--nb-border-default);
  margin-top: var(--nb-space-2);
  padding: var(--nb-space-2) 0;
  flex-shrink: 0;
}

.nb-tool-section-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  width: 100%;
  margin: 0;
  padding: var(--nb-space-1) var(--nb-space-1) var(--nb-space-2);
  background: transparent;
  border: none;
  cursor: pointer;
  color: var(--nb-text-secondary);
  font: inherit;
}

.nb-tool-section-label {
  font-size: var(--nb-fs-secondary);
  text-transform: uppercase;
  letter-spacing: 0.04em;
}

.nb-tool-section-head ion-icon {
  font-size: 14px;
  color: var(--nb-text-secondary);
}

/* C2: compact inline chips — one wrapping flex row, tight gap */
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

/* C3: one bordered composer group — textarea + Send bottom-right inside it */
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
  transition: border-color 120ms ease;
}

.nb-composer-surface:focus-within {
  border-color: var(--nb-teal);
}

.nb-composer-input {
  --background: transparent;
  --padding-start: var(--nb-space-2);
  --padding-end: var(--nb-space-2);
  --padding-top: 0;
  --padding-bottom: 0;
  font-size: var(--nb-fs-body);
}

.nb-composer-send {
  display: flex;
  justify-content: flex-end;
  align-items: center;
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
