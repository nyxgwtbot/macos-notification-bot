<template>
  <!-- R8: the page root carries a template ref — a failed save whose only
       violations mapped to inline fields (no banner) scrolls the first
       errored input anywhere on the page into view instead -->
  <ion-page ref="pageRoot">
    <ion-header>
      <ion-toolbar class="nb-page-header">
        <!-- R8: the page title is a real heading (level 1) — the shared
             .nb-page-header pattern's a11y contract, kept in every view -->
        <ion-title role="heading" aria-level="1">Settings</ion-title>
        <ion-buttons slot="end">
          <!-- R7 dirty-guard: disabled while pristine; dirty = "Discard edits?"
               (first click arms), armed = "Confirm discard?" (second click
               performs the discard/reload — same double-arm as the restart) -->
          <ion-button
            class="nb-reload-discard"
            :class="{ 'nb-discard-armed': dirtyGuard.armed }"
            :disabled="discardDisabled"
            :aria-label="discardLabel"
            :title="discardLabel"
            @click="onDiscardClick"
          >
            <ion-icon aria-hidden="true" :icon="refreshOutline" />
            <span v-if="dirtyGuard.dirty" class="nb-discard-label">{{ discardLabel }}</span>
          </ion-button>
        </ion-buttons>
      </ion-toolbar>
    </ion-header>

    <ion-content class="nb-content">
      <!-- R7: save/restart announces — visually-hidden polite region fed by
           save() and the confirmed restart (exact strings in the dirty VM) -->
      <p class="nb-sr-only" aria-live="polite">{{ srSaveAnnouncement }}</p>

      <ion-spinner v-if="store.configLoading && !store.config" name="crescent" class="nb-center" />

      <div v-if="store.configError && !store.config" class="nb-banner nb-banner-err">
        <span>Failed to load config: {{ store.configError }}</span>
        <ion-button size="small" fill="outline" @click="discard">Retry</ion-button>
      </div>

      <!-- Bot & Ollama: the app's own provider config — first card of Settings -->
      <ion-card v-if="store.config">
        <ion-card-header><ion-card-title>Bot &amp; Ollama</ion-card-title></ion-card-header>
        <ion-card-content>
          <p class="nb-muted">
            Provider the in-app agent uses for chat, classification and finance insights.
          </p>
          <div class="nb-field">
            <div class="nb-field-slot nb-span-2">
              <ion-input
                label="Ollama URL"
                label-placement="stacked"
                :class="{ 'nb-input-bad': !!fieldErrors.ollamaUrl }"
                :aria-invalid="fieldErrors.ollamaUrl ? true : undefined"
                :value="draft.ollamaUrl"
                @ionInput="onOllamaUrlInput"
              />
              <p v-if="fieldErrors.ollamaUrl" class="nb-field-error">{{ fieldErrors.ollamaUrl }}</p>
              <p class="nb-field-help">Endpoint of the local Ollama daemon.</p>
            </div>
            <div class="nb-field-slot">
              <ion-input
                label="Ollama model"
                label-placement="stacked"
                :class="{ 'nb-input-bad': !!fieldErrors.ollamaModel }"
                :aria-invalid="fieldErrors.ollamaModel ? true : undefined"
                :value="draft.ollamaModel"
                @ionInput="onOllamaModelInput"
              />
              <p v-if="fieldErrors.ollamaModel" class="nb-field-error">{{ fieldErrors.ollamaModel }}</p>
              <p class="nb-field-help">Model used for chat, classification and router calls (cloud-tagged models are proxied).</p>
            </div>
            <div class="nb-field-slot">
              <ion-input
                label="Ollama timeout (s)"
                label-placement="stacked"
                type="number"
                inputmode="decimal"
                :class="{ 'nb-input-bad': !!fieldErrors.ollamaTimeout }"
                :aria-invalid="fieldErrors.ollamaTimeout ? true : undefined"
                :value="draft.ollamaTimeout"
                @ionInput="onOllamaTimeoutInput"
              />
              <p v-if="fieldErrors.ollamaTimeout" class="nb-field-error">{{ fieldErrors.ollamaTimeout }}</p>
              <p class="nb-field-help">Seconds per provider call.</p>
            </div>
            <div class="nb-field-slot">
              <ion-input
                label="router.min_confidence (0–1)"
                label-placement="stacked"
                type="number"
                inputmode="decimal"
                :class="{ 'nb-input-bad': !!fieldErrors.minConfidence }"
                :aria-invalid="fieldErrors.minConfidence ? true : undefined"
                :value="draft.minConfidence"
                @ionInput="onMinConfidenceInput"
              />
              <p v-if="fieldErrors.minConfidence" class="nb-field-error">{{ fieldErrors.minConfidence }}</p>
              <p class="nb-field-help">Confidence floor for LLM-routed actions.</p>
            </div>
            <div class="nb-field-slot">
              <ion-input
                label="poll_interval (s)"
                label-placement="stacked"
                type="number"
                inputmode="decimal"
                :class="{ 'nb-input-bad': !!fieldErrors.pollInterval }"
                :aria-invalid="fieldErrors.pollInterval ? true : undefined"
                :value="draft.pollInterval"
                @ionInput="onPollIntervalInput"
              />
              <p v-if="fieldErrors.pollInterval" class="nb-field-error">{{ fieldErrors.pollInterval }}</p>
              <p class="nb-field-help">Seconds between daemon polls.</p>
            </div>
          </div>
          <!-- iterC: unmapped server 400 details never surface verbatim
               (api.py details are PT-BR) — one muted generic line instead -->
          <p v-if="unmappedSaveDetails.length > 0" class="nb-muted nb-save-reject-note">
            The server rejected the config — check the values
          </p>
          <!-- ST4: SAVE lives in the card footer row, aligned right (it saves the whole config);
               success = transient "Saved" flash (2s, label back) — validation is inline per field.
               iterC restart-to-apply: tinted banner next to SAVE, only when an ollama/router/poll
               value changed vs the last-loaded config. -->
          <!-- R8: the save-blocking banner renders NEXT TO the row that saves
               (the old page-bottom placement could sit a full scroll away from
               SAVE); a failed save scrolls it into view — reduced-motion aware
               via the shared scrollBehavior() helper (ref: validationBanner) -->
          <div v-if="validationBannerErrors.length > 0" ref="validationBanner" class="nb-banner nb-banner-err nb-save-validation-banner">
            <div>
              <strong>Fix before saving:</strong>
              <ul class="nb-details">
                <li v-for="detail in validationBannerErrors" :key="detail">{{ detail }}</li>
              </ul>
            </div>
          </div>
          <div class="nb-card-footer nb-save-row">
            <span v-if="store.lastSavedAt" class="nb-saved-at">saved at {{ formatIsoShort(store.lastSavedAt) }}</span>
            <div v-if="showRestartRow" class="nb-restart-apply">
              <span class="nb-restart-apply-label" title="The daemon reads config only at boot">
                Restart the daemon to apply
              </span>
              <ion-button
                size="small"
                fill="outline"
                class="nb-restart-btn"
                :class="{ 'nb-restart-armed': restartArmed }"
                :disabled="store.daemonBusy"
                aria-label="Restart daemon to apply the saved config"
                @click="onSaveRestartClick"
              >
                <ion-spinner v-if="store.daemonBusy" name="crescent" />
                <template v-else>{{ restartArmed ? 'Confirm restart?' : 'Restart' }}</template>
              </ion-button>
            </div>
            <ion-button fill="solid" class="nb-save-btn" :class="{ 'nb-save-flash': saveFlash }" :disabled="store.configSaving" @click="save">
              <template v-if="store.configSaving">
                <ion-spinner name="crescent" />
                <span class="nb-saving-label">Saving…</span>
              </template>
              <template v-else>{{ saveFlash ? 'Saved' : 'Save' }}</template>
            </ion-button>
          </div>
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
          <!-- R8: the error banner carries a Retry (same affordance as the
               config-load banner above) re-raising loadConnectors -->
          <div v-if="connectors.error" class="nb-banner nb-banner-err">
            <span>{{ connectors.error }}</span>
            <ion-button size="small" fill="outline" @click="loadConnectors">Retry</ion-button>
          </div>
        </ion-card-content>
      </ion-card>

      <template v-if="store.config">
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
                  :aria-label="'Enable action ' + entry.id"
                  @ionChange="onActionToggle($event as CustomEvent<{ checked: boolean }>, index)"
                />
              </div>
              <!-- R8: the action rows join the boxed input treatment of the
                   config fields (elevated box + focus ring); the empty-command
                   field error wears the standard pair (.nb-input-bad +
                   aria-invalid + inline message) and blocks the save -->
              <ion-textarea
                class="nb-action-box"
                label="match_hint"
                label-placement="stacked"
                auto-grow
                :value="entry.match_hint"
                @ionInput="onActionHintInput($event as IonValueEvent, index)"
              />
              <template v-if="entry.type === 'command'">
                <ion-input
                  class="nb-action-box nb-action-command"
                  label="command"
                  label-placement="stacked"
                  :class="{ 'nb-input-bad': actionErrors[index] !== undefined }"
                  :aria-invalid="actionErrors[index] !== undefined ? true : undefined"
                  :value="entry.command"
                  @ionInput="onActionCommandInput($event as IonValueEvent, index)"
                />
                <p v-if="actionErrors[index] !== undefined" class="nb-field-error">{{ actionErrors[index] }}</p>
              </template>
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
                :class="{ 'nb-raw-bad': !!rawError }"
                aria-label="JSON rules"
                :auto-grow="true"
                :rows="14"
                :value="draft.rawRules"
                @ionInput="onRawRulesInput($event as IonValueEvent)"
              />
              <!-- R6: clamped to 2 lines (shared .nb-error-line); full text in title -->
              <div v-if="rawError" class="nb-error-line" :title="rawError">{{ rawError }}</div>
            </template>
          </ion-card-content>
        </ion-card>

        <!-- R8: the save-blocking validation banner moved into the Bot &
             Ollama card, right above the save row; only the restart notice
             stays at the page bottom -->
        <div v-if="restartNote" class="nb-banner nb-banner-warn">{{ restartNote }}</div>
      </template>

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
    </ion-content>
  </ion-page>
</template>

<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, reactive, ref, watch, type ComponentPublicInstance } from 'vue';
import { onBeforeRouteLeave } from 'vue-router';
import {
  IonBadge,
  IonButton,
  IonButtons,
  alertController,
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
import {
  operationallyRelevantChange,
  splitDetailsByField,
  validateConfigDraft,
  validateDraftActionCommands,
  validateRuleConditionKeys,
  validateDraftFields,
  type ConfigDraftFields,
  type ConfigFieldKey
} from '@/services/configValidation';
import { formatIsoShort } from '@/utils/time';
import { scrollBehavior } from '@/utils/motion';
import {
  DIRTY_GUARD_CLEAN,
  RESTART_APPLY_ANNOUNCE,
  SAVE_FAILED_ANNOUNCE,
  SETTINGS_SAVED_ANNOUNCE,
  configDraftSnapshot,
  draftIsDirty,
  reduceDirtyGuard,
  type DirtyGuardEvent,
  type DirtyGuardState
} from '@/viewModels/configDirtyViewModel';
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
/** Inline field errors (red border + message under the input): merged from
 * the client pre-check and the server 400 details[]. */
const fieldErrors = ref<Partial<Record<ConfigFieldKey, string>>>({});
/** R8: per-index inline errors for the Actions rows (empty command on a
 * type:'command' action) — index-keyed against draft.actions. */
const actionErrors = ref<Record<number, string>>({});
/** R8: the save-blocking banner (inside the Bot & Ollama card) + the page
 * root (scroll fallback when every violation mapped to an inline field). */
const validationBanner = ref<HTMLElement | null>(null);
const pageRoot = ref<ComponentPublicInstance | null>(null);
/** Success flash: "Saved" tint on the button for 2s, then label back. */
const saveFlash = ref(false);
let saveFlashTimer: number | null = null;
/** Post-save restart affordance: double-arm like the Dashboard button, no dialog. */
const RESTART_ARM_MS = 3000;
const restartArmed = ref(false);
const restartPending = ref(false);
let restartArmTimer: number | null = null;

/** Config values as last loaded (set in buildDraft only — not on save):
 * the reference for "operationally relevant change" after a save, since the
 * daemon keeps running on the loaded values until it restarts. */
const loadedFields = ref<ConfigDraftFields>({
  ollamaUrl: '',
  ollamaModel: '',
  ollamaTimeout: '',
  minConfidence: '',
  pollInterval: ''
});

/* ========================================
   R7 DIRTY GUARD — the canonical draft snapshot is taken at load (buildDraft)
   and after a successful save; every edit re-serializes the draft against it.
   The header reload button turns into the discard double-arm while dirty and
   onBeforeRouteLeave confirms with an ion-alert. State machine: reduceDirtyGuard.
   ======================================== */
const dirtyGuard = ref<DirtyGuardState>({ ...DIRTY_GUARD_CLEAN });
const DISCARD_ARM_MS = 3000;
const draftSnapshot = ref<string | null>(null);

function markDraftSnapshot(): void {
  draftSnapshot.value = configDraftSnapshot(draft);
}

const dirtyNow = computed(() => draftIsDirty(draftSnapshot.value, draft));

watch(dirtyNow, (dirty) => dispatchDirtyGuard({ type: 'dirty-change', dirty }));

function dispatchDirtyGuard(event: DirtyGuardEvent): void {
  dirtyGuard.value = reduceDirtyGuard(dirtyGuard.value, event);
}

let discardArmTimer: number | null = null;

function clearDiscardArmTimer(): void {
  if (discardArmTimer !== null) {
    window.clearTimeout(discardArmTimer);
    discardArmTimer = null;
  }
}

function disarmDiscard(): void {
  clearDiscardArmTimer();
  dispatchDirtyGuard({ type: 'disarm' });
}

const discardDisabled = computed(
  () => store.configLoading || store.configSaving || !dirtyGuard.value.dirty
);

const discardLabel = computed(() => {
  if (!dirtyGuard.value.dirty) return 'Reload settings';
  return dirtyGuard.value.armed ? 'Confirm discard?' : 'Discard edits?';
});

function onDiscardClick(): void {
  if (discardDisabled.value) return;
  if (dirtyGuard.value.armed) {
    disarmDiscard();
    discard(); // rebuilds the draft from the freshly loaded config (re-snapshots)
    return;
  }
  dispatchDirtyGuard({ type: 'arm' });
  clearDiscardArmTimer();
  discardArmTimer = window.setTimeout(disarmDiscard, DISCARD_ARM_MS);
}

/** R7: leaving with unsaved edits confirms through an inline ion-alert. */
onBeforeRouteLeave(async () => {
  if (!dirtyGuard.value.dirty) return true;
  const alert = await alertController.create({
    header: 'Discard unsaved settings?',
    buttons: [
      { text: 'Cancel', role: 'cancel' },
      { text: 'Leave', role: 'destructive' }
    ]
  });
  await alert.present();
  const { role } = await alert.onDidDismiss();
  return role === 'destructive';
});

/* ========================================
   R7 SAVE/RESTART ANNOUNCES — one visually-hidden polite region; the
   clear-then-fill makes identical repeats ("Save failed." twice in a row)
   mutate the live node and re-announce (Dashboard discipline).
   ======================================== */
const srSaveAnnouncement = ref('');

async function announceSave(text: string): Promise<void> {
  srSaveAnnouncement.value = '';
  await nextTick();
  srSaveAnnouncement.value = text;
}

const unmappedSaveDetails = computed<string[]>(() => {
  // server 400 leftovers with no field match: rendered as one muted generic
  // line (verbatim details never reach the UI — api.py phrases them PT-BR)
  return splitDetailsByField(store.configSaveErrors).unmapped;
});
const validationBannerErrors = computed<string[]>(() => splitDetailsByField(validationErrors.value).unmapped);

const restartBridge = computed(() => typeof window !== 'undefined' && !!window.notifyAPI?.daemon);
const showRestartRow = computed(() => restartBridge.value && restartPending.value);

function clearSaveFlashTimer(): void {
  if (saveFlashTimer !== null) {
    window.clearTimeout(saveFlashTimer);
    saveFlashTimer = null;
  }
}

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

function onSaveRestartClick(): void {
  if (!restartArmed.value) {
    restartArmed.value = true;
    clearRestartArmTimer();
    restartArmTimer = window.setTimeout(disarmRestart, RESTART_ARM_MS);
    return;
  }
  disarmRestart();
  // banner stays up through the busy spinner and dismisses when the restart
  // completes; a failed restart keeps the affordance visible for a retry
  void store.restartDaemon().then((status) => {
    if (status === null) return;
    // R7: the confirmed restart reaches the announce region
    void announceSave(RESTART_APPLY_ANNOUNCE);
    restartPending.value = false;
    return store.pollDaemon();
  });
}

function clearFieldError(key: ConfigFieldKey): void {
  if (fieldErrors.value[key] !== undefined) {
    const next = { ...fieldErrors.value };
    delete next[key];
    fieldErrors.value = next;
  }
}

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
  loadedFields.value = {
    ollamaUrl: draft.ollamaUrl,
    ollamaModel: draft.ollamaModel,
    ollamaTimeout: draft.ollamaTimeout,
    minConfidence: draft.minConfidence,
    pollInterval: draft.pollInterval
  };
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
  fieldErrors.value = {};
  actionErrors.value = {};
  // R7: a rebuilt draft IS the reference — pristine until the next edit
  markDraftSnapshot();
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

const onOllamaUrlInput = (event: IonValueEvent) => {
  draft.ollamaUrl = textOf(event);
  clearFieldError('ollamaUrl');
};
const onOllamaModelInput = (event: IonValueEvent) => {
  draft.ollamaModel = textOf(event);
  clearFieldError('ollamaModel');
};
const onOllamaTimeoutInput = (event: IonValueEvent) => {
  draft.ollamaTimeout = textOf(event);
  clearFieldError('ollamaTimeout');
};
const onMinConfidenceInput = (event: IonValueEvent) => {
  draft.minConfidence = textOf(event);
  clearFieldError('minConfidence');
};
const onPollIntervalInput = (event: IonValueEvent) => {
  draft.pollInterval = textOf(event);
  clearFieldError('pollInterval');
};

function onActionToggle(event: CustomEvent<{ checked: boolean }>, index: number): void {
  if (draft.actions[index]) draft.actions[index].enabled = event.detail.checked;
}

function onActionHintInput(event: IonValueEvent, index: number): void {
  if (draft.actions[index]) draft.actions[index].match_hint = textOf(event);
}

function onActionCommandInput(event: IonValueEvent, index: number): void {
  if (!draft.actions[index]) return;
  draft.actions[index].command = textOf(event);
  // R8: typing clears this row's inline command error (same live rule as the
  // five config fields)
  if (actionErrors.value[index] !== undefined) {
    const next = { ...actionErrors.value };
    delete next[index];
    actionErrors.value = next;
  }
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
  fieldErrors.value = {};
  actionErrors.value = {};
  disarmRestart();
  restartPending.value = false;
  if (store.config) {
    void store.loadConfig().then(() => {
      if (store.config) buildDraft(store.config);
    });
  }
}

/** R8: a failed save brings the blocking errors into view — the banner next
 * to the save row first; when every violation mapped to an inline field (no
 * banner), the first errored input. 'nearest' avoids any page jump, and the
 * behavior degrades to 'auto' under prefers-reduced-motion (utils/motion). */
async function scrollToSaveFailure(): Promise<void> {
  await nextTick();
  const banner = validationBanner.value;
  const fallback =
    pageRoot.value !== null
      ? ((pageRoot.value as { $el?: Element }).$el?.querySelector('.nb-input-bad') ?? null)
      : null;
  const target: Element | null = banner ?? fallback;
  if (!target) return;
  try {
    target.scrollIntoView({ block: 'nearest', behavior: scrollBehavior() });
  } catch {
    // pre-options engines (jsdom, old webviews): skip the scroll, keep the save
  }
}

async function save(): Promise<void> {
  let payload: BotConfig;
  try {
    // Raw-mode rules must land in the draft first.
    showStructuredRules();
    if (rawError.value) {
      validationErrors.value = [`JSON rules: ${rawError.value}`];
      void scrollToSaveFailure();
      return;
    }
    payload = buildPayload();
  } catch (err) {
    validationErrors.value = [err instanceof Error ? err.message : String(err)];
    void scrollToSaveFailure();
    return;
  }
  // Inline-only validation (no browser alert): client rules first, then the
  // full server-mirror pass, both mapped onto the offending fields.
  const clientErrors = validateConfigDraft(payload);
  const mappedClient = splitDetailsByField(clientErrors).fieldErrors;
  const fieldChecks = validateDraftFields({
    ollamaUrl: draft.ollamaUrl,
    ollamaModel: draft.ollamaModel,
    ollamaTimeout: draft.ollamaTimeout,
    minConfidence: draft.minConfidence,
    pollInterval: draft.pollInterval
  });
  fieldErrors.value = { ...mappedClient, ...fieldChecks };
  // R8: the Actions rows join the draft validation — an empty command on a
  // command action is a per-row field error (inline box + message) and blocks
  // the round-trip exactly like the five config fields
  actionErrors.value = validateDraftActionCommands(draft.actions);
  validationErrors.value = clientErrors;
  // pre-existing blocking rule stays: any client-mirror violation (fields,
  // actions, rules…) blocks the round-trip; non-field leftovers hit the banner
  // (R8: rendered next to the save row), actions/field errors go inline
  if (
    clientErrors.length > 0 ||
    Object.keys(fieldChecks).length > 0 ||
    Object.keys(actionErrors.value).length > 0
  ) {
    void scrollToSaveFailure();
    return;
  }
  const ok = await store.saveConfig(payload);
  if (ok) {
    // success: previous inline errors are resolved — clean state
    fieldErrors.value = {};
    actionErrors.value = {};
    validationErrors.value = [];
    // R7: the saved state becomes the new pristine reference
    markDraftSnapshot();
    // R7 announce
    void announceSave(SETTINGS_SAVED_ANNOUNCE);
    // success: transient "Saved" flash (2s, label back)
    clearSaveFlashTimer();
    saveFlash.value = true;
    saveFlashTimer = window.setTimeout(() => {
      saveFlash.value = false;
      saveFlashTimer = null;
    }, 2000);
    // restart-to-apply (iterC): only when an ollama/router/poll value changed
    // vs the last-loaded config; values still equal -> no banner
    const changed = operationallyRelevantChange(loadedFields.value, {
      ollamaUrl: draft.ollamaUrl,
      ollamaModel: draft.ollamaModel,
      ollamaTimeout: draft.ollamaTimeout,
      minConfidence: draft.minConfidence,
      pollInterval: draft.pollInterval
    });
    if (changed) {
      if (restartBridge.value) {
        disarmRestart();
        restartPending.value = true;
      } else {
        restartNote.value =
          'Config saved. Restart the daemon manually (`python3 bot.py`) — process manager is available only in the Electron app.';
      }
    }
  } else {
    // server 400 fallback: map field-named details[] onto the offending fields
    // (EN text from the same table); non-field details collapse into the one
    // muted generic line (unmappedSaveDetails) — never verbatim PT-BR
    fieldErrors.value = { ...fieldErrors.value, ...splitDetailsByField(store.configSaveErrors).fieldErrors };
    // R8: the failed round-trip scrolls the blocking errors into view (the
    // unmapped leftovers may open the banner; mapped ones clear it and the
    // fallback scrolls the first errored input)
    void scrollToSaveFailure();
    // R7 announce
    void announceSave(SAVE_FAILED_ANNOUNCE);
  }
}

onMounted(async () => {
  const connectorsTask = loadConnectors(); // independent of the config draft
  await store.loadConfig();
  if (store.config) buildDraft(store.config);
  await connectorsTask;
});

onUnmounted(() => {
  clearSaveFlashTimer();
  disarmRestart();
  clearDiscardArmTimer();
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
   strong hairline, radius, focus ring; labels stay small/muted (Ion stacked).
   R4: 2-column grid — Ollama URL spans both columns (row 1); model + timeout
   pair on row 2, router.min_confidence + poll_interval on row 3 (R4: the old
   1-col 520px stack inside a ~1100px card is gone). */
.nb-field {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: var(--nb-space-2) var(--nb-space-4);
}
.nb-field .nb-span-2 {
  grid-column: 1 / -1;
}
/* R4: one-line muted help under each config field */
.nb-field-help {
  margin: var(--nb-space-1) 0 0;
  font-size: var(--nb-fs-micro);
  color: var(--nb-text-secondary);
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
/* R8: action rows join the boxed input treatment of the config fields —
   elevated surface, hairline, radius, focus ring (same pair as
   .nb-field ion-input) — and the invalid state wears the same danger pair */
.nb-action-row .nb-action-box {
  background: var(--nb-bg-elevated);
  border: 1px solid var(--nb-border-strong);
  border-radius: var(--nb-radius-md);
  padding: var(--nb-space-2) var(--nb-space-3);
  transition: border-color 120ms ease, box-shadow 120ms ease;
}
.nb-action-row .nb-action-box:focus-within {
  border-color: var(--nb-info);
  box-shadow: 0 0 0 2px var(--nb-info-muted);
}
.nb-action-row .nb-action-box.nb-input-bad {
  border-color: var(--nb-danger);
  background-color: var(--nb-danger-muted);
}
.nb-action-row .nb-action-box.nb-input-bad:focus-within {
  border-color: var(--nb-danger);
  box-shadow: 0 0 0 2px var(--nb-danger-muted);
}
/* command reads as shell text: same mono as the raw JSON editor */
.nb-action-row .nb-action-command {
  font-family: var(--nb-mono);
  font-size: var(--nb-fs-secondary);
}
/* labels above the values (ion-input internals are light-DOM-scoped) */
.nb-action-row :deep(.label-text-wrapper) {
  font-size: var(--nb-fs-secondary);
  color: var(--nb-text-secondary);
  margin-bottom: var(--nb-space-1);
}
/* inline message under the errored action input (shared .nb-field-error DNA) */
.nb-action-row .nb-field-error {
  margin: var(--nb-space-1) 0 0;
  padding-left: var(--nb-space-1);
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
/* R4: raw JSON mode parity — same boxed input treatment + focus ring as the
   config fields, monospace kept; while a raw parse error is live the textarea
   wears the same danger pair (error line stays inline under the editor) */
.nb-raw-json {
  font-family: var(--nb-mono);
  font-size: var(--nb-fs-micro);
  background: var(--nb-bg-elevated);
  border: 1px solid var(--nb-border-strong);
  border-radius: var(--nb-radius-md);
  padding: var(--nb-space-2) var(--nb-space-3);
  transition: border-color 120ms ease, box-shadow 120ms ease;
}
.nb-raw-json:focus-within {
  border-color: var(--nb-info);
  box-shadow: 0 0 0 2px var(--nb-info-muted);
}
.nb-raw-json.nb-raw-bad {
  border-color: var(--nb-danger);
  background-color: var(--nb-danger-muted);
}
.nb-raw-json.nb-raw-bad:focus-within {
  border-color: var(--nb-danger);
  box-shadow: 0 0 0 2px var(--nb-danger-muted);
}
/* ST4: right-aligned save footer inside the Bot & Ollama card
   (.nb-card-footer supplies the 12px top inset) */
.nb-save-row {
  justify-content: flex-end;
  gap: var(--nb-space-3);
}
/* inline field validation: danger border + subtle bg tint on the offending
   input + message line below it (iterD: live-verified the HOST border does turn
   danger — the earlier "neutral rgb(232,238,245)" reading was the native <input>'s
   currentColor, not the host border) */
.nb-field-slot {
  display: flex;
  flex-direction: column;
  gap: 0;
}
.nb-field .nb-input-bad {
  border-color: var(--nb-danger);
  background-color: var(--nb-danger-muted);
}
.nb-field .nb-input-bad:focus-within {
  border-color: var(--nb-danger);
  box-shadow: 0 0 0 2px var(--nb-danger-muted);
}
/* iterE: a11y-attribute parity — the standard [aria-invalid] hook drives the
   same live-verified host-box styling as .nb-input-bad (the visible box is the
   ion-input host; the native input inside shadow has no border of its own) */
.nb-field ion-input[aria-invalid="true"] {
  border-color: var(--nb-danger);
  background-color: var(--nb-danger-muted);
}
.nb-field-error {
  margin: 0 0 var(--nb-space-1);
  padding-left: var(--nb-space-1);
  font-size: var(--nb-fs-micro);
  color: var(--ion-color-danger);
}
/* success flash: ok tint pair on the save button for 2s */
.nb-save-flash {
  --background: var(--nb-chip-ok-bg);
  --color: var(--nb-chip-ok-fg);
  --border-width: 1px;
  --border-style: solid;
  --border-color: var(--nb-green);
}
/* iterC restart-to-apply: tinted banner row next to SAVE (shown only when an
   operationally relevant value changed); double-arm inside (armed = filled tint) */
.nb-restart-apply {
  display: flex;
  align-items: center;
  gap: var(--nb-space-2);
  background: var(--nb-chip-warn-bg);
  border: 1px solid var(--nb-warning);
  border-radius: var(--nb-radius-md);
  padding: var(--nb-space-1) var(--nb-space-2);
}
.nb-restart-apply-label {
  font-size: var(--nb-fs-secondary);
  color: var(--nb-chip-warn-fg);
  white-space: nowrap;
}
.nb-restart-apply .nb-restart-armed {
  --background: var(--nb-chip-warn-bg);
  --border-color: var(--nb-warning);
  --color: var(--nb-chip-warn-fg);
  --box-shadow: none;
}
.nb-saved-at {
  font-size: var(--nb-fs-secondary);
  color: var(--nb-text-secondary);
}
/* iterC: single muted line for unmapped server 400 details (no verbatim PT-BR) */
.nb-save-reject-note {
  margin: var(--nb-space-1) 0 0;
  font-size: var(--nb-fs-secondary);
}

/* R7: pending save label beside the spinner */
.nb-saving-label {
  margin-left: var(--nb-space-1);
}

/* R7 dirty-guard: the header reload button while dirty carries the discard
   label; armed state wears the restart warn pair so "confirm" reads the same
   affordance everywhere */
.nb-reload-discard.nb-discard-armed {
  --color: var(--nb-chip-warn-fg);
  --background: transparent;
  --border-color: var(--nb-warning);
  --border-style: solid;
  --border-width: 1px;
  --border-radius: var(--nb-radius-sm);
  --box-shadow: none;
}
.nb-discard-label {
  font-size: var(--nb-fs-micro);
  font-weight: 600;
}
</style>
