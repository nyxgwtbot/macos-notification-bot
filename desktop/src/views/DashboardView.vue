<template>
  <ion-page>
    <ion-header>
      <ion-toolbar>
        <ion-title>Painel</ion-title>
      </ion-toolbar>
    </ion-header>

    <ion-content class="nb-content">
      <!-- offline banner: API down -> explicit banner + retry, never blank -->
      <div v-if="store.apiDown" class="nb-offline">
        <ion-spinner v-if="store.feedLoading" name="crescent" />
        <ion-icon aria-hidden="true" :icon="cloudOfflineOutline" />
        <span>API offline: {{ store.feedError }}</span>
        <ion-button size="small" fill="outline" @click="retryPolls">Tentar de novo</ion-button>
      </div>

      <div class="nb-cards">
        <!-- Daemon card: process managed by Electron main (not via the API) -->
        <ion-card>
          <ion-card-header>
            <ion-card-title>Daemon do bot</ion-card-title>
            <ion-badge :color="daemonBadgeColor">{{ daemonBadgeText }}</ion-badge>
          </ion-card-header>
          <ion-card-content>
            <template v-if="store.daemon">
              <div class="nb-kv"><span>PID</span><strong>{{ store.daemon.pid ?? '—' }}</strong></div>
              <div class="nb-kv"><span>Uptime</span><strong>{{ formatUptime(store.daemon.uptimeSec) || '—' }}</strong></div>
              <div class="nb-kv"><span>Iniciado</span><strong>{{ formatIsoShort(store.daemon.startedAt) || '—' }}</strong></div>
              <div class="nb-kv"><span>Porta API</span><strong>{{ store.daemon.port ?? '—' }}</strong></div>
              <div class="nb-kv"><span>Adotado</span><strong>{{ store.daemon.adopted ? 'sim (instância externa)' : 'não (gerenciado aqui)' }}</strong></div>
              <div v-if="store.daemon.lastError" class="nb-error-line">Erro: {{ store.daemon.lastError }}</div>
            </template>
            <template v-else>
              <p class="nb-muted">
                {{ store.daemonError === 'bridge-ausente' ? 'Process manager indisponível no modo navegador (vite). Gerencie o daemon via `python3 bot.py`.' : store.daemonError || 'Sem status do daemon.' }}
              </p>
            </template>
            <ion-button
              size="small"
              fill="outline"
              :disabled="store.daemonBusy || !daemonBridge"
              @click="onRestartDaemon"
            >
              <ion-spinner v-if="store.daemonBusy" name="crescent" />
              <template v-else>Reiniciar daemon</template>
            </ion-button>
          </ion-card-content>
        </ion-card>

        <!-- Health card: watermark + state + ollama (from GET /api/health) -->
        <ion-card>
          <ion-card-header>
            <ion-card-title>Estado do bot</ion-card-title>
            <ion-badge :color="store.healthError ? 'danger' : 'success'">
              {{ store.healthError ? 'erro' : 'ok' }}
            </ion-badge>
          </ion-card-header>
          <ion-card-content>
            <template v-if="store.health">
              <div class="nb-kv"><span>Watermark (last_rec_id)</span><strong>{{ store.health.watermark.last_rec_id }}</strong></div>
              <div class="nb-kv"><span>Último ciclo (state.updated_at)</span><strong>{{ formatIsoShort(store.health.watermark.updated_at) || '—' }}</strong></div>
              <div class="nb-kv"><span>Ollama</span><strong>{{ store.health.ollama.url }}</strong></div>
              <div class="nb-kv"><span>Modelo</span><strong>{{ store.health.ollama.model }}</strong></div>
              <div class="nb-kv"><span>Log</span><strong>{{ formatBytes(store.health.log.bytes) }} · v{{ store.health.bot_version }}/api v{{ store.health.api_version }}</strong></div>
              <div class="nb-kv"><span>Finance</span><strong>{{ store.health.finance.enabled ? (store.health.finance.db_exists ? 'habilitado' : 'habilitado (sem ledger)') : 'desabilitado' }}</strong></div>
            </template>
            <template v-else>
              <p class="nb-muted">
                <ion-spinner v-if="store.healthLoading" name="crescent" />
                <span v-else>Sem resposta de /api/health{{ store.healthError ? `: ${store.healthError}` : '…' }}</span>
              </p>
            </template>
          </ion-card-content>
        </ion-card>
      </div>

      <!-- Live feed -->
      <div class="nb-feed-header">
        <h2>Eventos ao vivo</h2>
        <ion-badge :color="store.feedError ? 'danger' : 'primary'">
          <ion-icon aria-hidden="true" :icon="pulseOutline" />
          {{ store.feedError ? 'offline' : 'a cada 2s' }}
        </ion-badge>
      </div>

      <ion-spinner v-if="store.feedLoading && store.events.length === 0" name="crescent" class="nb-center" />

      <p v-else-if="!store.apiDown && store.events.length === 0" class="nb-empty">
        Nenhum evento ainda — o bot ainda não registrou nenhuma notificação. Use a aba <strong>Config</strong> para regras ou espere um evento do macOS.
      </p>

      <div v-if="store.events.length > 0" class="nb-feed">
        <div v-for="row in feedRows" :key="row.key" class="nb-event" :class="{ 'nb-event-startup': row.kind === 'startup' }">
          <div class="nb-event-top">
            <span class="nb-app">{{ row.app }}</span>
            <span class="nb-when">{{ formatIsoShort(row.timestamp) }}</span>
            <span v-if="row.recId !== null" class="nb-recid">rec_id {{ row.recId }}</span>
          </div>
          <div class="nb-event-title">{{ row.kind === 'startup' ? 'inicialização' : row.title || '(sem título)' }}</div>
          <template v-if="row.kind === 'notification'">
            <div class="nb-event-summary">{{ row.summary }}</div>
            <div class="nb-chips">
              <ion-chip :color="categoryColor">{{ row.category }}</ion-chip>
              <ion-chip :color="urgencyColor(row.urgency)">{{ row.urgency }}</ion-chip>
              <ion-badge v-if="row.layer" :color="layerBadgeColor(row.layer)" class="nb-badge">{{ row.layerLabel }}</ion-badge>
              <ion-badge v-if="row.ruleMatched" color="secondary" class="nb-badge">regra: {{ row.ruleMatched }}</ion-badge>
              <ion-badge v-if="row.confidencePct" color="tertiary" class="nb-badge">confiança {{ row.confidencePct }}</ion-badge>
              <ion-badge v-if="row.telegramStatus" :color="telegramBadgeColor(row.telegramStatus)" class="nb-badge">telegram {{ row.telegramStatus }}</ion-badge>
              <ion-badge v-if="row.financeStatus" :color="financeBadgeColor(row.financeStatus)" class="nb-badge">finance {{ row.financeStatus }}</ion-badge>
            </div>
            <div v-if="row.actions.length > 0" class="nb-actions">
              <span v-for="action in row.actions" :key="action" class="nb-action">{{ action }}</span>
            </div>
          </template>
        </div>
      </div>
    </ion-content>
  </ion-page>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted } from 'vue';
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
import { cloudOfflineOutline, pulseOutline } from 'ionicons/icons';

import { useBotStore } from '@/stores/bot';
import { usePolling } from '@/composables/usePolling';
import { formatBytes, formatIsoShort, formatUptime } from '@/utils/time';

const store = useBotStore();

const daemonBridge = computed(() => typeof window !== 'undefined' && !!window.notifyAPI?.daemon);

const feedRows = computed(() => [...store.events].reverse());

const daemonBadgeColor = computed(() =>
  store.daemon?.running ? (store.daemon.adopted ? 'secondary' : 'success') : 'medium'
);
const daemonBadgeText = computed(() => {
  if (store.daemon) return store.daemon.running ? (store.daemon.adopted ? 'rodando (adotado)' : 'rodando') : 'parado';
  return store.daemonError === 'bridge-ausente' ? 'bridge ausente' : 'sem status';
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

function onRestartDaemon() {
  void store.restartDaemon().then(() => store.pollDaemon());
}

function retryPolls() {
  void store.loadHealth();
  void store.pollFeed();
}

const feedPoller = usePolling(() => store.pollFeed(), { intervalMs: 2000 });
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
});
</script>

<style scoped>
.nb-content {
  padding: 12px;
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
.nb-cards {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
  gap: 12px;
  margin-bottom: 12px;
}
.nb-kv {
  display: flex;
  justify-content: space-between;
  gap: 8px;
  font-size: 13px;
  padding: 2px 0;
}
.nb-kv span {
  color: var(--nb-text-secondary, var(--ion-color-medium));
}
.nb-error-line {
  margin-top: 6px;
  font-size: 12px;
  color: var(--ion-color-danger);
}
.nb-muted {
  font-size: 13px;
  color: var(--nb-text-secondary, var(--ion-color-medium));
}
.nb-feed-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin: 4px 4px 10px;
}
.nb-feed-header h2 {
  margin: 0;
  font-size: 17px;
}
.nb-center {
  display: block;
  margin: 32px auto;
  width: 32px;
  height: 32px;
}
.nb-empty {
  padding: 24px 8px;
  color: var(--nb-text-secondary, var(--ion-color-medium));
  text-align: center;
}
.nb-feed {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.nb-event {
  border: 1px solid var(--nb-border-default, #2a3544);
  border-radius: 10px;
  padding: 10px 12px;
  background: var(--ion-card-background);
}
.nb-event-startup {
  opacity: 0.65;
}
.nb-event-top {
  display: flex;
  gap: 10px;
  align-items: baseline;
  font-size: 12px;
  color: var(--nb-text-secondary, var(--ion-color-medium));
}
.nb-app {
  font-weight: 600;
  color: var(--nb-teal-light, var(--ion-color-primary));
}
.nb-when,
.nb-recid {
  color: var(--nb-text-muted, var(--ion-color-medium));
}
.nb-event-title {
  font-weight: 600;
  margin-top: 2px;
}
.nb-event-summary {
  color: var(--nb-text-secondary, var(--ion-color-medium));
  font-size: 13px;
  margin-top: 2px;
}
.nb-chips {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  margin-top: 8px;
  align-items: center;
}
.nb-badge {
  font-size: 11px;
}
.nb-actions {
  margin-top: 6px;
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
}
.nb-action {
  font-family: var(--nb-mono, monospace);
  font-size: 11px;
  padding: 2px 6px;
  border-radius: 6px;
  background: var(--nb-purple-muted, rgba(163, 113, 217, 0.15));
}
</style>