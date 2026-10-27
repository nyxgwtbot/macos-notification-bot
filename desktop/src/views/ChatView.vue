<template>
  <ion-page>
    <ion-header>
      <ion-toolbar>
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
          <ion-button size="small" fill="outline" @click="newSession">New chat</ion-button>
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
              {{ bubble.text }}
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

          <!-- Tool activity: compact chips in flow before composer -->
          <div v-if="state.chips.length > 0" class="nb-tool-activity">
            <h3>Tool activity</h3>
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

          <!-- Turn error banner with retry -->
          <div v-if="state.error" class="nb-error-strip">
            <div class="nb-error-strip-msg">
              <ion-icon aria-hidden="true" :icon="alertCircleOutline" />
              <span>Turn failed: {{ state.error }}</span>
            </div>
            <ion-button size="small" fill="outline" @click="retryLast">Retry</ion-button>
          </div>

          <!-- Composer: pinned at bottom of main column, always visible -->
          <!-- Verified: Pane scroll model with --overflow:hidden on .nb-chat-content, .nb-transcript handles internal scroll (flex: 1 1 0; overflow-y: auto), composer is flex-shrink: 0 and stays pinned at the bottom left, preserving full visibility without page-level scroll. -->
          <div class="nb-composer">
            <ion-textarea
              v-model="draft"
              class="nb-composer-input"
              placeholder="Message the bot…"
              :auto-grow="true"
              :rows="1"
              @keydown="onComposerKeydown($event)"
            />
            <ion-button :disabled="!canSend" fill="solid" @click="send">
              <ion-spinner v-if="state.pending" name="crescent" />
              <template v-else>Send</template>
            </ion-button>
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

const state = ref<ChatState>(initialChatState());
const draft = ref('');
const loadError = ref<string | null>(null);
const lastMessage = ref<string | null>(null);
const expandedChips = reactive(new Set<string>());
const railOpen = ref(true);
const reloadKey = ref(0);
const copiedRecently = ref(false);
const transcriptEl = ref<HTMLElement | null>(null);

const canSend = computed(() => !state.value.pending && draft.value.trim() !== '');

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

.nb-session-select {
  min-height: 36px;
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

.nb-tool-activity {
  display: flex;
  flex-direction: column;
  gap: var(--nb-space-1);
  margin-top: var(--nb-space-2);
  margin-bottom: var(--nb-space-2);
  flex-shrink: 0;
}

.nb-tool-activity h3 {
  font-size: var(--nb-fs-secondary);
  text-transform: uppercase;
  letter-spacing: 0.04em;
  color: var(--nb-text-secondary);
  margin: 0 0 var(--nb-space-1);
}

.nb-tool-chip {
  border: 1px solid var(--nb-border-default);
  border-radius: var(--nb-radius-md);
  background: var(--ion-card-background);
}

.nb-tool-chip-error {
  border-color: var(--ion-color-danger);
}

.nb-tool-chip-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--nb-space-2);
  width: 100%;
  padding: var(--nb-space-1) var(--nb-space-2);
  background: transparent;
  border: none;
  cursor: pointer;
  color: inherit;
  font: inherit;
}

.nb-tool-tool {
  font-family: var(--nb-mono);
  font-size: var(--nb-fs-secondary);
}

.nb-tool-preview {
  margin: 0;
  max-height: 220px;
  overflow-y: auto;
  overflow-x: auto;
  padding: 0 var(--nb-space-2) var(--nb-space-2);
  font-family: var(--nb-mono);
  font-size: var(--nb-fs-micro);
  line-height: 1.45;
  color: var(--nb-text-secondary);
}

.nb-composer {
  display: flex;
  gap: var(--nb-space-2);
  border-top: 1px solid var(--nb-border-default);
  padding-top: var(--nb-space-2);
  margin-top: var(--nb-space-2);
  align-items: flex-end;
  flex-shrink: 0;
}

.nb-composer-input {
  flex: 1;
  border: 1px solid var(--nb-border-default);
  border-radius: var(--nb-radius-md);
  padding: var(--nb-space-2) var(--nb-space-3);
  --background: transparent;
  font-size: var(--nb-fs-body);
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
