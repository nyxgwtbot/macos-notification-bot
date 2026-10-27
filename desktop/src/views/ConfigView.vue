<template>
  <ion-page>
    <ion-header>
      <ion-toolbar>
        <ion-title>Settings</ion-title>
        <ion-buttons slot="end">
          <ion-button :disabled="store.configLoading || store.configSaving" @click="discard">
            <ion-icon aria-hidden="true" :icon="refreshOutline" />
          </ion-button>
        </ion-buttons>
      </ion-toolbar>
    </ion-header>

    <ion-content class="nb-content">
      <ion-spinner v-if="store.configLoading && !store.config" name="crescent" class="nb-center" />

      <div v-if="store.configError && !store.config" class="nb-banner nb-banner-err">
        <span>Erro ao carregar config: {{ store.configError }}</span>
        <ion-button size="small" fill="outline" @click="discard">Tentar de novo</ion-button>
      </div>

      <!-- Telegram: strictly read-only, secrets never enter config.json via the UI -->
      <ion-card>
        <ion-card-header><ion-card-title>Telegram (env-only)</ion-card-title></ion-card-header>
        <ion-card-content>
          <p class="nb-muted">
            Tokens NÃO são configuráveis aqui: eles vêm de variáveis de ambiente —
            <code>TELEGRAM_BOT_TOKEN</code> ou <code>TG_BOT_TOKEN</code> e
            <code>TELEGRAM_ALLOWED_USERS</code> ou <code>TG_CHAT_ID</code>. Segredos nunca entram em
            <code>config.json</code> pela UI (o servidor rejeita valores não vazios nesses campos).
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
                <ion-badge :color="statusChipColor(row.status)">{{ statusChipLabel(row.status) }}</ion-badge>
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
          </ion-card-content>
        </ion-card>

        <!-- action registry -->
        <ion-card>
          <ion-card-header><ion-card-title>Registro de ações</ion-card-title></ion-card-header>
          <ion-card-content>
            <div v-for="(entry, index) in draft.actions" :key="entry.id || index" class="nb-action-row">
              <div class="nb-action-head">
                <strong>{{ entry.id }}</strong>
                <ion-badge color="medium">{{ entry.type }}</ion-badge>
                <ion-toggle
                  :checked="entry.enabled"
                  aria-label="habilitar ação"
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
          <ion-card-header>
            <ion-card-title>Regras determinísticas</ion-card-title>
            <div class="nb-mode-switch">
              <ion-button
                size="small"
                :fill="draft.mode === 'structured' ? 'solid' : 'outline'"
                @click="showStructuredRules"
              >Tabela</ion-button>
              <ion-button
                size="small"
                :fill="draft.mode === 'raw' ? 'solid' : 'outline'"
                @click="showRawRules"
              >JSON</ion-button>
            </div>
          </ion-card-header>
          <ion-card-content>
            <template v-if="draft.mode === 'structured'">
              <p v-if="draft.rules.length === 0" class="nb-muted">Nenhuma regra — todas as notificações vão para o Router LLM.</p>
              <div v-for="(rule, index) in draft.rules" :key="index" class="nb-rule-row">
                <div class="nb-rule-name">{{ rule.name || '(sem nome)' }}</div>
                <div class="nb-rule-cond">
                  <span v-for="(value, key) in rule.condition" :key="key">
                    <code>{{ key }}</code> = {{ condValueText(value) }}
                  </span>
                  <span v-if="!rule.condition || Object.keys(rule.condition).length === 0" class="nb-muted">sempre verdadeira</span>
                </div>
                <div class="nb-rule-actions">→ {{ rule.actions.join(' + ') }}</div>
              </div>
            </template>
            <template v-else>
              <ion-textarea
                class="nb-raw-json"
                aria-label="regras em JSON"
                :auto-grow="true"
                :rows="14"
                :value="draft.rawRules"
                @ionInput="onRawRulesInput($event as IonValueEvent)"
              />
              <div v-if="rawError" class="nb-error-line">{{ rawError }}</div>
            </template>
          </ion-card-content>
        </ion-card>

        <!-- save + errors -->
        <div class="nb-save-row">
          <ion-button fill="solid" :disabled="store.configSaving" @click="save">
            <ion-spinner v-if="store.configSaving" name="crescent" />
            <template v-else>Salvar</template>
          </ion-button>
          <span v-if="store.lastSavedAt" class="nb-muted">salvo em {{ formatIsoShort(store.lastSavedAt) }}</span>
        </div>

        <div v-if="validationErrors.length > 0" class="nb-banner nb-banner-err">
          <div>
            <strong>Corrija antes de salvar:</strong>
            <ul class="nb-details">
              <li v-for="detail in validationErrors" :key="detail">{{ detail }}</li>
            </ul>
          </div>
        </div>
        <div v-if="store.configSaveErrors.length > 0" class="nb-banner nb-banner-err">
          <div>
            <strong>Servidor rejeitou (400):</strong>
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
  IonPage,
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
  statusChipColor,
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
  if (!base) throw new Error('config não carregada');
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
      rawError.value = `JSON inválido: ${err instanceof Error ? err.message : String(err)}`;
      return;
    }
  }
  draft.mode = 'structured';
}

function onRawRulesInput(event: IonValueEvent): void {
  draft.rawRules = textOf(event);
  try {
    JSON.parse(draft.rawRules);
    rawError.value = null;
  } catch (err) {
    rawError.value = `JSON inválido: ${err instanceof Error ? err.message : String(err)}`;
  }
}

function condValueText(value: string | string[]): string {
  return Array.isArray(value) ? value.join(' ou ') : String(value);
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
      'Config salva. Reinicie o daemon manualmente (python3 bot.py) — o process manager só existe no app Electron.';
    return;
  }
  const alert = await alertController.create({
    header: 'Reiniciar daemon?',
    message: 'A config é lida apenas no boot do daemon. Reiniciar agora para aplicar as mudanças?',
    buttons: [
      { text: 'Mais tarde', role: 'cancel' },
      {
        text: 'Reiniciar',
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
      validationErrors.value = [`regras em JSON: ${rawError.value}`];
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
.nb-content {
  padding: 12px;
}
.nb-banner {
  display: flex;
  align-items: flex-start;
  gap: 10px;
  margin: 0 4px 12px;
  padding: 10px 14px;
  border-radius: 10px;
}
.nb-banner-err {
  background: var(--nb-danger-muted, rgba(248, 81, 73, 0.15));
  color: var(--ion-color-danger);
}
.nb-banner-warn {
  background: var(--nb-warning-muted, rgba(210, 153, 34, 0.15));
  color: var(--ion-color-warning);
}
.nb-details {
  margin: 6px 0 0;
  padding-left: 18px;
  font-size: 12px;
}
.nb-connector-row {
  border-bottom: 1px dashed var(--nb-border-subtle, #1e2833);
  padding: 8px 0;
}
.nb-connector-row:last-of-type {
  border-bottom: none;
}
.nb-connector-head {
  display: flex;
  align-items: center;
  gap: 10px;
}
.nb-connector-hint {
  margin: 2px 0 4px;
  font-size: 12px;
}
.nb-connectors-loading {
  display: block;
  margin: 12px auto;
  width: 24px;
  height: 24px;
}
.nb-field {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.nb-action-row {
  border-bottom: 1px dashed var(--nb-border-subtle, #1e2833);
  padding-bottom: 6px;
  margin-bottom: 10px;
}
.nb-action-head {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 4px;
}
.nb-mode-switch {
  display: flex;
  gap: 4px;
}
.nb-rule-row {
  padding: 4px 0;
  border-bottom: 1px solid var(--nb-border-subtle, #1e2833);
  font-size: 13px;
}
.nb-rule-name {
  font-weight: 600;
}
.nb-rule-cond code {
  font-family: var(--nb-mono, monospace);
  background: var(--nb-bg-elevated, #262f3c);
  padding: 0 4px;
  border-radius: 4px;
}
.nb-raw-json {
  font-family: var(--nb-mono, monospace);
  font-size: 12px;
}
.nb-save-row {
  display: flex;
  align-items: center;
  gap: 12px;
  margin: 8px 4px 16px;
}
.nb-muted {
  color: var(--nb-text-secondary, var(--ion-color-medium));
  font-size: 13px;
}
.nb-error-line {
  color: var(--ion-color-danger);
  font-size: 12px;
  margin-top: 6px;
}
.nb-center {
  display: block;
  margin: 32px auto;
  width: 32px;
  height: 32px;
}
</style>