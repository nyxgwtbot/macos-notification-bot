<template>
  <ion-page>
    <ion-header>
      <ion-toolbar>
        <ion-title>Chat</ion-title>
      </ion-toolbar>
    </ion-header>

    <ion-content class="nb-content">
      <!-- offline banner: sessions endpoint unreachable -> banner + retry, never blank -->
      <div v-if="loadError" class="nb-offline">
        <ion-icon aria-hidden="true" :icon="cloudOfflineOutline" />
        <span>API offline: {{ loadError }}</span>
        <ion-button size="small" fill="outline" @click="loadSessions">Tentar de novo</ion-button>
      </div>

      <!-- session picker row -->
      <div class="nb-session-row">
        <ion-select
          class="nb-session-select"
          interface="popover"
          placeholder="Select session"
          :value="state.sessionId ?? ''"
          @ionChange="onSessionChange($event)"
        >
          <ion-select-option v-for="session in state.sessions" :key="session.session_id" :value="session.session_id">
            {{ session.preview }} ({{ session.turns }} turns)
          </ion-select-option>
        </ion-select>
        <ion-button size="small" fill="outline" @click="newSession">New session</ion-button>
      </div>

      <!-- transcript bubbles -->
      <div class="nb-transcript">
        <div
          v-for="bubble in state.messages"
          :key="bubble.id"
          class="nb-bubble"
          :class="bubble.role === 'user' ? 'nb-bubble-user' : 'nb-bubble-assistant'"
        >
          {{ bubble.text }}
        </div>
        <p v-if="state.messages.length === 0 && !state.pending" class="nb-empty">
          Ask the bot to build a dashboard, send test notifications, or summarize its data —
          the agent runs local tools and replies here.
        </p>
        <div v-if="state.pending" class="nb-bubble nb-bubble-assistant">
          <ion-spinner name="crescent" />
        </div>
      </div>

      <!-- tool activity: one expandable chip per turn step (hydranote tool-card pattern) -->
      <div v-if="state.chips.length > 0" class="nb-tool-activity">
        <h3>Tool activity</h3>
        <div v-for="chip in state.chips" :key="chip.id" class="nb-tool-chip" :class="{ 'nb-tool-chip-error': !chip.ok }">
          <button type="button" class="nb-tool-chip-head" @click="toggleChip(chip.id)">
            <span class="nb-tool-tool">{{ chip.tool }}</span>
            <ion-badge :color="chip.ok ? 'success' : 'danger'">{{ chip.ok ? 'ok' : 'error' }}</ion-badge>
          </button>
          <pre v-if="expandedChips.has(chip.id)" class="nb-tool-preview">{{ chip.preview }}</pre>
        </div>
      </div>

      <!-- inline error with retry -->
      <div v-if="state.error" class="nb-error-line">
        <ion-icon aria-hidden="true" :icon="alertCircleOutline" />
        <span>{{ state.error }}</span>
        <ion-button size="small" fill="outline" @click="retryLast">Retry</ion-button>
      </div>

      <!-- artifact iframe (sandbox attr empty = no scripts; URL allowlisted in the view-model) -->
      <ion-card v-if="state.artifactUrl" class="nb-artifact">
        <ion-card-header>
          <ion-card-title>Dashboard</ion-card-title>
        </ion-card-header>
        <ion-card-content class="nb-artifact-content">
          <iframe class="nb-artifact-frame" sandbox="" :src="state.artifactUrl" title="Generated dashboard"></iframe>
        </ion-card-content>
      </ion-card>

      <!-- composer -->
      <div class="nb-composer">
        <ion-input
          v-model="draft"
          class="nb-composer-input"
          placeholder="Message the bot…"
          @keyup.enter="send"
        />
        <ion-button :disabled="!canSend" fill="solid" @click="send">
          <ion-spinner v-if="state.pending" name="crescent" />
          <template v-else>Send</template>
        </ion-button>
      </div>
    </ion-content>
  </ion-page>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue';
import {
  IonBadge,
  IonButton,
  IonCard,
  IonCardContent,
  IonCardHeader,
  IonCardTitle,
  IonContent,
  IonHeader,
  IonIcon,
  IonInput,
  IonPage,
  IonSelect,
  IonSelectOption,
  IonSpinner,
  IonTitle,
  IonToolbar
} from '@ionic/vue';
import { alertCircleOutline, cloudOfflineOutline } from 'ionicons/icons';

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

const canSend = computed(() => !state.value.pending && draft.value.trim() !== '');

function dispatch(event: ChatEvent): void {
  state.value = reduceChat(state.value, event);
}

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
.nb-content {
  padding: 12px;
  display: flex;
  flex-direction: column;
}
.nb-offline {
  display: flex;
  align-items: center;
  gap: 10px;
  margin: 0 4px 12px;
  padding: 10px 14px;
  border-radius: 10px;
  background: var(--nb-danger-muted, rgba(248, 81, 73, 0.15));
  color: var(--ion-color-danger);
}
.nb-session-row {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 12px;
}
.nb-session-select {
  flex: 1;
  min-width: 0;
  border: 1px solid var(--nb-border-default, #2a3544);
  border-radius: 8px;
  padding: 6px 10px;
}
.nb-transcript {
  display: flex;
  flex-direction: column;
  gap: 8px;
  margin-bottom: 12px;
}
.nb-bubble {
  max-width: 82%;
  padding: 8px 12px;
  border-radius: 14px;
  font-size: 14px;
  line-height: 1.45;
  white-space: pre-wrap;
  word-break: break-word;
}
.nb-bubble-user {
  align-self: flex-end;
  background: var(--nb-teal-muted, rgba(63, 185, 141, 0.18));
  border: 1px solid var(--nb-teal-light, var(--ion-color-primary));
}
.nb-bubble-assistant {
  align-self: flex-start;
  background: var(--ion-card-background);
  border: 1px solid var(--nb-border-default, #2a3544);
}
.nb-empty {
  padding: 24px 8px;
  color: var(--nb-text-secondary, var(--ion-color-medium));
  text-align: center;
  align-self: center;
}
.nb-tool-activity {
  margin-bottom: 12px;
}
.nb-tool-activity h3 {
  font-size: 14px;
  margin: 0 0 6px;
  color: var(--nb-text-secondary, var(--ion-color-medium));
}
.nb-tool-chip {
  border: 1px solid var(--nb-border-default, #2a3544);
  border-radius: 10px;
  margin-bottom: 6px;
  background: var(--ion-card-background);
}
.nb-tool-chip-error {
  border-color: var(--ion-color-danger, #f85149);
}
.nb-tool-chip-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  width: 100%;
  padding: 8px 10px;
  background: transparent;
  border: none;
  cursor: pointer;
  color: inherit;
  font: inherit;
}
.nb-tool-tool {
  font-family: var(--nb-mono, monospace);
  font-size: 12px;
}
.nb-tool-preview {
  margin: 0;
  padding: 0 10px 10px;
  font-size: 11px;
  overflow-x: auto;
  color: var(--nb-text-secondary, var(--ion-color-medium));
}
.nb-error-line {
  display: flex;
  align-items: center;
  gap: 10px;
  margin: 0 0 12px;
  padding: 10px 14px;
  border-radius: 10px;
  background: var(--nb-danger-muted, rgba(248, 81, 73, 0.15));
  color: var(--ion-color-danger);
  font-size: 13px;
}
.nb-artifact {
  margin-bottom: 12px;
}
.nb-artifact-content {
  padding: 0;
}
.nb-artifact-frame {
  width: 100%;
  height: 420px;
  border: none;
  border-radius: 8px;
  background: #ffffff;
}
.nb-composer {
  display: flex;
  gap: 8px;
  align-items: center;
  margin-top: auto;
  padding-top: 8px;
}
.nb-composer-input {
  flex: 1;
  border: 1px solid var(--nb-border-default, #2a3544);
  border-radius: 10px;
  padding: 8px 12px;
  --background: transparent;
}
</style>