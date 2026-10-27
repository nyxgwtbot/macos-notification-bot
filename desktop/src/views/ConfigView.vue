<template>
  <ion-page>
    <ion-header>
      <ion-toolbar class="nb-page-header">
        <ion-title>Settings</ion-title>
        <ion-buttons slot="end">
          <ion-button :disabled="store.configLoading || store.configSaving" aria-label="Reload settings" title="Reload settings" @click="discard">
            <ion-icon aria-hidden="true" :icon="refreshOutline" />
          </ion-button>
        </ion-buttons>
      </ion-toolbar>
    </ion-header>

    <ion-content class="nb-content">
      <ion-spinner v-if="store.configLoading && !store.config" name="crescent" class="nb-center" />

      <div v-if="store.configError && !store.config" class="nb-banner nb-banner-err">
        <span>Failed to load config: {{ store.configError }}</span>
        <ion-button size="small" fill="outline" @click="discard">Retry</ion-button>
      </div>

      <!-- Telegram: strictly read-only, secrets never enter config.json via the UI -->
      <ion-card>
        <ion-card-header><ion-card-title>Telegram (env-only)</ion-card-title></ion-card-header>
        <ion-card-content>
          <p class="nb-muted">
            Tokens are not editable here: they come from environment variables —
            <code>TELEGRAM_BOT_TOKEN</code>/<code>TG_BOT_TOKEN</code> and
            <code>TELEGRAM_ALLOWED_USERS</code>/<code>TG_CHAT_ID</code>. Secrets never enter
            <code>config.json</code> through the UI (the server rejects non-empty values for these fields).
          </p>
        </ion-card-content>
      </ion-card>

      <!-- Connectors: native-app consent + probe (agent's native.call tool) -->
      <ion-card>
        <ion-card-header><ion-card-title>Connectors</ion-card-title></ion-card-header>
        <ion-card-content>
          <p class="nb-muted">
            Connectors let the in-app agent (Chat tab) control native macOS apps. macOS may show a
            system consent prompt — approve it while this app is focused.
          </p>
          <template v-if="connectors.rows.length > 0">
            <div v-for="row in connectors.rows" :key="row.domain" class="nb-connector-row">
              <div class="nb-connector-head">
                <strong>{{ row.domain }}</strong>
                <span class="nb-status-chip" :class="'nb-status-' + statusChipTone(row.status)">
                  <i class="nb-dot" aria-hidden="true"></i>{{ statusChipLabel(row.status) }}
                </span>
                <ion-button
                  size="small"
                  fill="outline"
                  :disabled="isProbingConnectors(row.domain)"
                  :aria-label="(row.granted ? 'Re-check ' : 'Connect ') + row.domain"
                  @click="probeConnector(row.domain)"
                >
                  <ion-spinner v-if="isProbingConnectors(row.domain)" name="crescent" />
                  <template v-else>{{ buttonLabel(row) }}</template>
                </ion-button>
              </div>
              <p class="nb-muted nb-connector-hint">{{ row.hint }}</p>
            </div>
          </template>
          <ion-spinner v-else name="crescent" class="nb-connectors-loading" />
          <div v-if="connectors.error" class="nb-banner nb-banner-err">
            <span>{{ connectors.error }}</span>
          </div>
        </ion-card-content>
      </ion-card>

      <template v-if="store.config">
        <!-- structured editors -->
        <ion-card>
          <ion-card-header><ion-card-title>Bot &amp; Ollama</ion-card-title></ion-card-header>
          <ion-card-content>
            <div class="nb-field">
              <ion-input
                label="Ollama URL"
                label-placement="stacked"
                :value="draft.ollamaUrl"
                @ionInput="onOllamaUrlInput"
              />
              <ion-input
                label="Ollama model"
                label-placement="stacked"
                :value="draft.ollamaModel"
                @ionInput="onOllamaModelInput"
              />
              <ion-input
                label="Ollama timeout (s)"
                label-placement="stacked"
                type="number"
                inputmode="decimal"
                :value="draft.ollamaTimeout"
                @ionInput="onOllamaTimeoutInput"
              />
              <ion-input
                label="router.min_confidence (0–1)"
                label-placement="stacked"
                type="number"
                inputmode="decimal"
                :value="draft.minConfidence"
                @ionInput="onMinConfidenceInput"
              />
              <ion-input
                label="poll_interval (s)"
                label-placement="stacked"
                type="number"
                inputmode="decimal"
                :value="draft.pollInterval"
                @ionInput="onPollIntervalInput"
              />
            </div>
            <!-- ST4: SAVE lives in the card footer row, aligned right (it saves the whole config) -->
            <div class="nb-card-footer nb-save-row">
              <span v-if="store.lastSavedAt" class="nb-saved-at">saved at {{ formatIsoShort(store.lastSavedAt) }}</span>
              <ion-button fill="solid" :disabled="store.configSaving" @click="save">
                <ion-spinner v-if="store.configSaving" name="crescent" />
                <template v-else>Save</template>
              </ion-button>
            </div>
          </ion-card-content>
        </ion-card>

        <!-- action registry -->
        <ion-card>
          <ion-card-header><ion-card-title>Actions</ion-card-title></ion-card-header>
          <ion-card-content>
            <div v-for="(entry, index) in draft.actions" :key="entry.id || index" class="nb-action-row">
              <div class="nb-action-head">
                <strong>{{ entry.id }}</strong>
                <ion-badge color="medium">{{ entry.type }}</ion-badge>
                <ion-toggle
                  :checked="entry.enabled"
                  aria-label="Enable action"
                  @ionChange="onActionToggle($event as CustomEvent<{ checked: boolean }>, index)"
                />
              </div>
              <ion-textarea
                label="match_hint"
                label-placement="stacked"
                auto-grow
                :value="entry.match_hint"
                @ionInput="onActionHintInput($event as IonValueEvent, index)"
              />
              <ion-input
                v-if="entry.type === 'command'"
                label="command"
                label-placement="stacked"
                :value="entry.command"
                @ionInput="onActionCommandInput($event as IonValueEvent, index)"
              />
            </div>
          </ion-card-content>
        </ion-card>

        <!-- rules: readable table <-> raw JSON editor -->
        <ion-card>
          <ion-card-header class="nb-rules-header">
            <ion-card-title>Rules</ion-card-title>
            <ion-segment
              class="nb-mode-segment"
              :value="draft.mode"
              @ionChange="onModeChange($event)"
            >
              <ion-segment-button value="structured">
                <ion-label>Table</ion-label>
              </ion-segment-button>
              <ion-segment-button value="raw">
                <ion-label>JSON</ion-label>
              </ion-segment-button>
            </ion-segment>
          </ion-card-header>
          <ion-card-content>
            <template v-if="draft.mode === 'structured'">
              <p v-if="draft.rules.length === 0" class="nb-muted">No rules — every notification goes to the LLM Router.</p>
              <div v-for="(rule, index) in draft.rules" :key="index" class="nb-rule-row">
                <div class="nb-rule-name">{{ rule.name || '(unnamed)' }}</div>
                <div class="nb-rule-cond">
                  <span v-for="(value, key) in rule.condition" :key="key">
                    <code>{{ key }}</code> = {{ condValueText(value) }}
                  </span>
                  <span v-if="!rule.condition || Object.keys(rule.condition).length === 0" class="nb-muted">always true</span>
                </div>
                <div class="nb-rule-actions">→ {{ rule.actions.join(' + ') }}</div>
              </div>
            </template>
            <template v-else>
              <ion-textarea
                class="nb-raw-json"
                aria-label="JSON rules"
                :auto-grow="true"
                :rows="14"
                :value="draft.rawRules"
                @ionInput="onRawRulesInput($event as IonValueEvent)"
              />
              <div v-if="rawError" class="nb-error-line">{{ rawError }}</div>
            </template>
          </ion-card-content>
        </ion-card>

        <!-- validation + server errors -->
        <div v-if="validationErrors.length > 0" class="nb-banner nb-banner-err">
          <div>
            <strong>Fix before saving:</strong>
            <ul class="nb-details">
              <li v-for="detail in validationErrors" :key="detail">{{ detail }}</li>
            </ul>
          </div>
        </div>
        <div v-if="store.configSaveErrors.length > 0" class="nb-banner nb-banner-err">
          <div>
            <strong>Server rejected the change (400):</strong>
            <ul class="nb-details">
              <li v-for="detail in store.configSaveErrors" :key="detail">{{ detail }}</li>
            </ul>
          </div>
        </div>
        <div v-if="restartNote" class="nb-banner nb-banner-warn">{{ restartNote }}</div>
      </template>
    </ion-content>
  </ion-page>
</template>

<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue';
import {
  alertController,
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
  IonInput,
  IonLabel,
  IonPage,
  IonSegment,
  IonSegmentButton,
  IonSpinner,
  IonTextarea,
  IonTitle,
  IonToggle,
  IonToolbar
} from '@ionic/vue';
import { refreshOutline } from 'ionicons/icons';

import { useBotStore } from '@/stores/bot';
import { notifyClient, errorText } from '@/services/notifyClient';
import { validateConfigDraft, validateRuleConditionKeys } from '@/services/configValidation';
import { formatIsoShort } from '@/utils/time';
import {
  buttonLabel,
  initialConnectorsState,
  isProbing,
  reduceConnectors,
  statusChipTone,
  statusChipLabel,
  type ConnectorsEvent,
  type ConnectorsState,
} from '@/viewModels/connectorsViewModel';
import type { BotConfig, BotRule } from '@/types/bot';

interface ActionDraft {
  id: string;
  type: 'builtin' | 'command';
  enabled: boolean;
  match_hint: string;
  command: string;
}

interface ConfigDraft {
  pollInterval: string;
  ollamaUrl: string;
  ollamaModel: string;
  ollamaTimeout: string;
  minConfidence: string;
  actions: ActionDraft[];
  rules: BotRule[];
  mode: 'structured' | 'raw';
  rawRules: string;
}

const store = useBotStore();

const connectors = ref<ConnectorsState>(initialConnectorsState());

function dispatchConnectors(event: ConnectorsEvent): void {
  connectors.value = reduceConnectors(connectors.value, event);
}

function isProbingConnectors(domain: string): boolean {
  return isProbing(connectors.value, domain);
}

async function loadConnectors(): Promise<void> {
  dispatchConnectors({ type: 'load-start' });
  try {
    const response = await notifyClient.getConnectors();
    dispatchConnectors({ type: 'rows-loaded', response });
  } catch (err) {
    dispatchConnectors({ type: 'load-failed', error: errorText(err) });
  }
}

async function probeConnector(domain: string): Promise<void> {
  // concurrent-probe guard: the button disables, but double-check anyway
  if (isProbing(connectors.value, domain)) return;
  dispatchConnectors({ type: 'probe-start', domain });
  try {
    const probe = await notifyClient.postConnectorProbe(domain);
    dispatchConnectors({
      type: 'probe-succeeded',
      domain,
      row: {
        status: probe.status,
        granted: probe.status === 'granted',
        last_probe: probe.last_probe,
        hint: '',
      },
    });
  } catch (err) {
    dispatchConnectors({ type: 'probe-failed', domain, error: errorText(err) });
  }
}

const draft = reactive<ConfigDraft>({
  pollInterval: '',
  ollamaUrl: '',
  ollamaModel: '',
  ollamaTimeout: '',
  minConfidence: '',
  actions: [],
  rules: [],
  mode: 'structured',
  rawRules: ''
});

const validationErrors = ref<string[]>([]);
const rawError = ref<string | null>(null);
const restartNote = ref<string | null>(null);

/** Ionic input/textarea events carry { value?: string | null }. */
type IonValueEvent = CustomEvent<{ value?: string | null }>;

function textOf(event: IonValueEvent): string {
  return String(event.detail?.value ?? '');
}

function buildDraft(config: BotConfig): void {
  draft.pollInterval = String(config.poll_interval);
  draft.ollamaUrl = config.ollama?.url ?? '';
  draft.ollamaModel = config.ollama?.model ?? '';
  draft.ollamaTimeout = String(config.ollama?.timeout ?? 30);
  draft.minConfidence = String(config.router?.min_confidence ?? 0.5);
  draft.actions = (config.actions ?? []).map((entry) => ({
    id: entry.id,
    type: entry.type === 'command' ? 'command' : 'builtin',
    enabled: entry.enabled,
    match_hint: entry.match_hint ?? '',
    command: String(entry.config?.command ?? '')
  }));
  draft.rules = JSON.parse(JSON.stringify(config.rules ?? [])) as BotRule[];
  draft.mode = 'structured';
  draft.rawRules = JSON.stringify(draft.rules, null, 2);
  rawError.value = null;
  validationErrors.value = [];
}

function buildPayload(): BotConfig {
  const base = store.config;
  if (!base) throw new Error('config not loaded');
  return {
    ...base,
    poll_interval: Number(draft.pollInterval),
    ollama: {
      ...base.ollama,
      url: draft.ollamaUrl.trim(),
      model: draft.ollamaModel.trim(),
      timeout: Number(draft.ollamaTimeout)
    },
    router: {
      ...base.router,
      min_confidence: Number(draft.minConfidence)
    },
    actions: draft.actions.map((entry, index) => {
      const original = base.actions[index];
      const configEntry = { ...(original?.config ?? {}) };
      if (entry.type === 'command') configEntry.command = entry.command.trim();
      return {
        id: entry.id.trim(),
        match_hint: entry.match_hint,
        type: entry.type,
        enabled: entry.enabled,
        config: configEntry
      };
    }),
    rules: draft.rules,
    // Secrets never return via the UI (env-only); server enforces this too.
    telegram: { token: '', chat_id: '' }
  };
}

const onOllamaUrlInput = (event: IonValueEvent) => (draft.ollamaUrl = textOf(event));
const onOllamaModelInput = (event: IonValueEvent) => (draft.ollamaModel = textOf(event));
const onOllamaTimeoutInput = (event: IonValueEvent) => (draft.ollamaTimeout = textOf(event));
const onMinConfidenceInput = (event: IonValueEvent) => (draft.minConfidence = textOf(event));
const onPollIntervalInput = (event: IonValueEvent) => (draft.pollInterval = textOf(event));

function onActionToggle(event: CustomEvent<{ checked: boolean }>, index: number): void {
  if (draft.actions[index]) draft.actions[index].enabled = event.detail.checked;
}

function onActionHintInput(event: IonValueEvent, index: number): void {
  if (draft.actions[index]) draft.actions[index].match_hint = textOf(event);
}

function onActionCommandInput(event: IonValueEvent, index: number): void {
  if (draft.actions[index]) draft.actions[index].command = textOf(event);
}

function showRawRules(): void {
  draft.rawRules = JSON.stringify(draft.rules, null, 2);
  rawError.value = null;
  draft.mode = 'raw';
}

function showStructuredRules(): void {
  if (draft.mode === 'raw') {
    try {
      const parsed = JSON.parse(draft.rawRules) as unknown;
      const problems = validateRuleConditionKeys(parsed as BotRule[]);
      if (problems.length > 0) {
        rawError.value = problems.join(' · ');
        return; // do not leave raw mode with an invalid draft
      }
      draft.rules = parsed as BotRule[];
      rawError.value = null;
    } catch (err) {
      rawError.value = `Invalid JSON: ${err instanceof Error ? err.message : String(err)}`;
      return;
    }
  }
  draft.mode = 'structured';
}

function onModeChange(event: CustomEvent): void {
  const nextMode = String(event.detail?.value || '');
  if (nextMode === 'raw') {
    showRawRules();
  } else if (nextMode === 'structured') {
    showStructuredRules();
  }
}

function onRawRulesInput(event: IonValueEvent): void {
  draft.rawRules = textOf(event);
  try {
    JSON.parse(draft.rawRules);
    rawError.value = null;
  } catch (err) {
    rawError.value = `Invalid JSON: ${err instanceof Error ? err.message : String(err)}`;
  }
}

function condValueText(value: string | string[]): string {
  return Array.isArray(value) ? value.join(' or ') : String(value);
}

function discard(): void {
  validationErrors.value = [];
  if (store.config) {
    void store.loadConfig().then(() => {
      if (store.config) buildDraft(store.config);
    });
  }
}

async function confirmRestart(): Promise<void> {
  const bridge = window.notifyAPI?.daemon;
  if (!bridge) {
    restartNote.value =
      'Config saved. Restart the daemon manually (`python3 bot.py`) — process manager is available only in the Electron app.';
    return;
  }
  const alert = await alertController.create({
    header: 'Restart daemon?',
    message: 'The daemon reads config only at boot. Restart now to apply the changes?',
    buttons: [
      { text: 'Later', role: 'cancel' },
      {
        text: 'Restart',
        role: 'destructive',
        handler: () => {
          void store.restartDaemon();
        }
      }
    ]
  });
  await alert.present();
}

async function save(): Promise<void> {
  let payload: BotConfig;
  try {
    // Raw-mode rules must land in the draft first.
    showStructuredRules();
    if (rawError.value) {
      validationErrors.value = [`JSON rules: ${rawError.value}`];
      return;
    }
    payload = buildPayload();
  } catch (err) {
    validationErrors.value = [err instanceof Error ? err.message : String(err)];
    return;
  }
  validationErrors.value = validateConfigDraft(payload);
  if (validationErrors.value.length > 0) return;
  const ok = await store.saveConfig(payload);
  if (ok) {
    await confirmRestart();
  }
}

onMounted(async () => {
  const connectorsTask = loadConnectors(); // independent of the config draft
  await store.loadConfig();
  if (store.config) buildDraft(store.config);
  await connectorsTask;
});
</script>

<style scoped>
.nb-details {
  margin: var(--nb-space-1) 0 0;
  padding-left: var(--nb-space-4);
  font-size: var(--nb-fs-secondary);
}
/* ST1: calm separators — solid hairline (was dashed) */
.nb-connector-row {
  border-bottom: 1px solid var(--nb-border-default);
  padding: var(--nb-space-2) 0;
}
.nb-connector-row:last-of-type {
  border-bottom: none;
}
.nb-connector-head {
  display: flex;
  align-items: center;
  gap: var(--nb-space-2);
}
.nb-connector-hint {
  margin: var(--nb-space-1) 0 0;
  font-size: var(--nb-fs-secondary);
}
.nb-connectors-loading {
  display: block;
  margin: var(--nb-space-3) auto;
  width: var(--nb-space-6);
  height: var(--nb-space-6);
}
/* ST2: Bot & Ollama fields read as editable inputs — elevated surface,
   strong hairline, radius, focus ring; labels stay small/muted (Ion stacked) */
.nb-field {
  display: flex;
  flex-direction: column;
  gap: var(--nb-space-2);
  max-width: 520px;
}
.nb-field ion-input {
  background: var(--nb-bg-elevated);
  border: 1px solid var(--nb-border-strong);
  border-radius: var(--nb-radius-md);
  padding: var(--nb-space-2) var(--nb-space-3);
  transition: border-color 120ms ease, box-shadow 120ms ease;
}
.nb-field ion-input:focus-within {
  border-color: var(--nb-info);
  box-shadow: 0 0 0 2px var(--nb-info-muted);
}
/* ST2: labels stay small + muted above the values (ion-input internals are
   light-DOM-scoped, so :deep() reaches them) */
.nb-field :deep(.label-text-wrapper) {
  font-size: var(--nb-fs-secondary);
  color: var(--nb-text-secondary);
  margin-bottom: var(--nb-space-1);
}
.nb-action-row {
  border-bottom: 1px solid var(--nb-border-default);
  padding-bottom: var(--nb-space-2);
  margin-bottom: var(--nb-space-3);
}
.nb-action-head {
  display: flex;
  align-items: center;
  gap: var(--nb-space-2);
  margin-bottom: var(--nb-space-1);
}
.nb-rules-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.nb-mode-segment {
  max-width: 200px;
}
.nb-rule-row {
  padding: var(--nb-space-1) 0;
  border-bottom: 1px solid var(--nb-border-subtle);
  font-size: var(--nb-fs-secondary);
}
.nb-rule-name {
  font-weight: 600;
}
.nb-rule-cond code {
  font-family: var(--nb-mono);
  background: var(--nb-bg-elevated);
  padding: 0 var(--nb-space-1);
  border-radius: var(--nb-radius-sm);
}
.nb-raw-json {
  font-family: var(--nb-mono);
  font-size: var(--nb-fs-micro);
}
/* ST4: right-aligned save footer inside the Bot & Ollama card
   (.nb-card-footer supplies the 12px top inset) */
.nb-save-row {
  justify-content: flex-end;
  gap: var(--nb-space-3);
}
.nb-saved-at {
  font-size: var(--nb-fs-secondary);
  color: var(--nb-text-secondary);
}
</style>
