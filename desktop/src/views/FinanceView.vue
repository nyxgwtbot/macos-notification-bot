<template>
  <ion-page>
    <ion-header>
      <ion-toolbar>
        <ion-title>Finanças</ion-title>
      </ion-toolbar>
    </ion-header>

    <ion-content class="nb-content">
      <!-- disabled state -->
      <div v-if="financeDisabled" class="nb-banner nb-banner-warn">
        <ion-icon aria-hidden="true" :icon="lockClosedOutline" />
        <span>Plugin finance desabilitado (config.finance.enabled = false). Habilite-o na aba <strong>Config</strong>.</span>
      </div>

      <!-- error banner -->
      <div v-if="store.financeError" class="nb-banner nb-banner-err">
        <span>{{ store.financeError }}</span>
        <ion-button size="small" fill="outline" @click="reload">Tentar de novo</ion-button>
      </div>

      <!-- month navigator -->
      <div class="nb-month-nav">
        <ion-button fill="clear" size="small" :disabled="financeDisabled" @click="gotoMonth(previousMonth(store.financeMonth))">
          <ion-icon aria-hidden="true" :icon="chevronBackOutline" />
        </ion-button>
        <ion-chip color="primary" :disabled="financeDisabled" @click="gotoMonth(currentMonth())">{{ store.financeMonth || currentMonth() }} · {{ monthLabelPt(store.financeMonth || currentMonth()) }}</ion-chip>
        <ion-button fill="clear" size="small" :disabled="financeDisabled" @click="gotoMonth(nextMonth(store.financeMonth))">
          <ion-icon aria-hidden="true" :icon="chevronForwardOutline" />
        </ion-button>
      </div>

      <ion-spinner v-if="store.financeLoading && !store.financeSummary" name="crescent" class="nb-center" />

      <template v-if="store.financeSummary">
        <!-- summary cards -->
        <div class="nb-summary-row" :class="{ 'nb-summary-row-disabled': financeDisabled }">
          <ion-card>
            <ion-card-header><ion-card-title>Total do mês</ion-card-title></ion-card-header>
            <ion-card-content>
              <div class="nb-total" :class="{ 'nb-total-zero': !store.financeSummary.db_exists }">
                {{ formatBrl(store.financeSummary.total) }}
              </div>
              <div v-if="!store.financeSummary.db_exists" class="nb-muted">Sem finance.db — resumo zerado.</div>
            </ion-card-content>
          </ion-card>
          <ion-card>
            <ion-card-header><ion-card-title>Mês anterior</ion-card-title></ion-card-header>
            <ion-card-content>
              <div class="nb-total nb-total-smaller">{{ formatBrl(store.financeSummary.prev_total) }}</div>
              <div class="nb-mom" :class="momClass">
                <template v-if="momLabel !== null">{{ momLabel }} vs mês anterior</template>
                <template v-else><span class="nb-muted">sem base de comparação</span></template>
              </div>
            </ion-card-content>
          </ion-card>
          <ion-card>
            <ion-card-header><ion-card-title>Transações</ion-card-title></ion-card-header>
            <ion-card-content>
              <div class="nb-total nb-total-smaller">{{ store.financeSummary.count }}</div>
              <div class="nb-muted" v-if="byKind">recebimentos: {{ byKind.recebimento ? formatBrl(byKind.recebimento) : '—' }}</div>
            </ion-card-content>
          </ion-card>
        </div>

        <!-- by-category inline bars (no chart lib) -->
        <ion-card v-if="categoryBars.length > 0">
          <ion-card-header><ion-card-title>Por categoria</ion-card-title></ion-card-header>
          <ion-card-content>
            <div v-for="bar in categoryBars" :key="bar.category" class="nb-cat-bar">
              <span class="nb-cat-label">{{ bar.category }}</span>
              <span class="nb-cat-track"><span class="nb-cat-fill" :style="{ width: bar.width + '%' }" /></span>
              <span class="nb-cat-value">{{ formatBrl(bar.value) }}</span>
            </div>
          </ion-card-content>
        </ion-card>

        <!-- ledger table -->
        <ion-card>
          <ion-card-header><ion-card-title>Lançamentos</ion-card-title></ion-card-header>
          <ion-card-content class="nb-ledger-wrap">
            <p v-if="store.financeTransactions.length === 0" class="nb-empty">Nenhuma transação registrada neste mês.</p>
            <table v-else class="nb-ledger">
              <thead>
                <tr><th>Data</th><th>Tipo</th><th>Categoria</th><th class="nb-num">Valor</th><th>Descrição</th><th>App</th></tr>
              </thead>
              <tbody>
                <tr v-for="tx in store.financeTransactions" :key="tx.rec_id">
                  <td>{{ formatIsoShort(tx.ts) }}</td>
                  <td><ion-badge :color="tx.kind === 'recebimento' ? 'success' : tx.kind === 'gasto' ? 'warning' : 'medium'">{{ tx.kind }}</ion-badge></td>
                  <td>{{ tx.category }}</td>
                  <td class="nb-num">{{ formatBrl(tx.amount) }}</td>
                  <td class="nb-desc">{{ tx.description || '—' }}</td>
                  <td class="nb-bundle">{{ tx.bundle || '—' }}</td>
                </tr>
              </tbody>
            </table>
          </ion-card-content>
        </ion-card>

        <!-- report generation -->
        <ion-card>
          <ion-card-header><ion-card-title>Relatório</ion-card-title></ion-card-header>
          <ion-card-content>
            <div class="nb-report-row">
              <ion-button fill="solid" :disabled="store.reportBusy || financeDisabled" @click="generate">
                <ion-spinner v-if="store.reportBusy" name="crescent" />
                <template v-else>{{ store.reportStatus?.exists ? '(Re)gerar relatório' : 'Gerar relatório' }}</template>
              </ion-button>
              <template v-if="reportPath">
                <ion-button fill="outline" :disabled="!reportOpenable" @click="openReport">Abrir no navegador</ion-button>
                <ion-button fill="outline" :disabled="!reportOpenable" @click="revealReport">Mostrar no Finder</ion-button>
              </template>
            </div>
            <div v-if="reportPath" class="nb-report-path">
              {{ reportPath }} {{ store.reportStatus?.exists && !store.reportResult ? '(já existe — clique em (Re)gerar para atualizar)' : '' }}
            </div>
            <div v-if="store.reportResult" class="nb-report-ok">
              <strong>Relatório gerado:</strong> {{ store.reportResult.html_path }}
            </div>
            <div v-if="store.reportError" class="nb-error-line">{{ store.reportError }}</div>
          </ion-card-content>
        </ion-card>
      </template>
    </ion-content>
  </ion-page>
</template>

<script setup lang="ts">
import { computed, onMounted } from 'vue';
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
import { chevronBackOutline, chevronForwardOutline, lockClosedOutline } from 'ionicons/icons';

import { useBotStore } from '@/stores/bot';
import {
  currentMonth,
  formatBrl,
  formatMomDelta,
  monthLabelPt,
  nextMonth,
  previousMonth
} from '@/utils/financeFormat';
import { formatIsoShort } from '@/utils/time';

const store = useBotStore();

const financeDisabled = computed(() => {
  if (store.health?.finance?.enabled === false) return true;
  return typeof store.financeError === 'string' && store.financeError.includes('finance está desabilitado');
});

const momLabel = computed(() => formatMomDelta(store.financeSummary?.mom_delta ?? null));
const momClass = computed(() => {
  const delta = store.financeSummary?.mom_delta ?? null;
  if (delta === null || delta === undefined) return 'nb-muted';
  return delta >= 0 ? 'nb-mom-up' : 'nb-mom-down';
});

const byKind = computed(() => store.financeSummary?.by_kind ?? undefined);

const categoryBars = computed(() => {
  const byCategory = store.financeSummary?.by_category ?? {};
  const items = Object.entries(byCategory).filter(([, value]) => value > 0);
  const max = Math.max(...items.map(([, value]) => value), 0);
  return items
    .sort((a, b) => b[1] - a[1])
    .map(([category, value]) => ({
      category,
      value,
      width: max > 0 ? Math.max((value / max) * 100, 4) : 4
    }));
});

const reportPath = computed(() => store.reportResult?.html_path ?? store.reportStatus?.html_path ?? '');
const reportOpenable = computed(() => {
  if (typeof window === 'undefined') return false;
  if (!window.notifyAPI?.report) return false; // browser dev: não abre arquivos locais
  return true;
});

function gotoMonth(month: string) {
  void store.loadFinance(month);
}

function reload() {
  void store.loadFinance(store.financeMonth || currentMonth());
}

async function generate() {
  await store.loadFinance(store.financeMonth || currentMonth());
  await store.buildReport(store.financeMonth || currentMonth());
}

function openReport() {
  const bridge = window.notifyAPI?.report;
  if (!bridge || !reportPath.value) return;
  void bridge.openExternal(reportPath.value).then((result) => {
    if (!result.success) console.warn('openExternal falhou:', result.error);
  });
}

function revealReport() {
  const bridge = window.notifyAPI?.report;
  if (!bridge || !reportPath.value) return;
  void bridge.revealInFinder(reportPath.value).then((result) => {
    if (!result.success) console.warn('revealInFinder falhou:', result.error);
  });
}

onMounted(async () => {
  await store.loadHealth();
  if (!financeDisabled.value) {
    await store.loadFinance(currentMonth());
  }
});
</script>

<style scoped>
.nb-content {
  padding: 12px;
}
.nb-banner {
  display: flex;
  align-items: center;
  gap: 10px;
  margin: 0 4px 12px;
  padding: 10px 14px;
  border-radius: 10px;
}
.nb-banner-warn {
  background: var(--nb-warning-muted, rgba(210, 153, 34, 0.15));
  color: var(--ion-color-warning);
}
.nb-banner-err {
  background: var(--nb-danger-muted, rgba(248, 81, 73, 0.15));
  color: var(--ion-color-danger);
}
.nb-month-nav {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  margin-bottom: 12px;
}
.nb-summary-row {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
  gap: 12px;
  margin-bottom: 12px;
}
.nb-summary-row-disabled {
  opacity: 0.6;
}
.nb-total {
  font-size: 26px;
  font-weight: 700;
  color: var(--ion-color-primary);
}
.nb-total-smaller {
  font-size: 20px;
}
.nb-total-zero {
  color: var(--ion-color-medium);
}
.nb-mom {
  font-size: 13px;
}
.nb-mom-up {
  color: var(--nb-danger, var(--ion-color-danger));
}
.nb-mom-down {
  color: var(--ion-color-success);
}
.nb-muted {
  color: var(--nb-text-secondary, var(--ion-color-medium));
  font-size: 13px;
}
.nb-cat-bar {
  display: grid;
  grid-template-columns: 130px 1fr 90px;
  align-items: center;
  gap: 10px;
  margin-bottom: 6px;
  font-size: 13px;
}
.nb-cat-track {
  display: block;
  height: 12px;
  border-radius: 6px;
  background: var(--nb-bg-elevated, #262f3c);
  overflow: hidden;
}
.nb-cat-fill {
  display: block;
  height: 100%;
  border-radius: 6px;
  background: var(--ion-color-primary);
}
.nb-cat-value {
  text-align: right;
}
.nb-center {
  display: block;
  margin: 32px auto;
  width: 32px;
  height: 32px;
}
.nb-empty {
  color: var(--nb-text-secondary, var(--ion-color-medium));
  text-align: center;
  padding: 16px 0;
}
.nb-ledger {
  width: 100%;
  border-collapse: collapse;
  font-size: 12px;
  font-family: var(--nb-mono, monospace);
}
.nb-ledger th,
.nb-ledger td {
  text-align: left;
  padding: 5px 8px;
  border-bottom: 1px solid var(--nb-border-subtle, #1e2833);
  white-space: nowrap;
}
.nb-ledger .nb-num {
  text-align: right;
}
.nb-ledger .nb-desc,
.nb-ledger .nb-bundle {
  max-width: 260px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.nb-ledger-wrap {
  overflow-x: auto;
}
.nb-report-row {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
}
.nb-report-path {
  margin-top: 8px;
  font-size: 12px;
  color: var(--nb-text-secondary, var(--ion-color-medium));
  font-family: var(--nb-mono, monospace);
  word-break: break-all;
}
.nb-report-ok {
  margin-top: 6px;
  font-size: 13px;
  color: var(--ion-color-success);
}
.nb-error-line {
  margin-top: 6px;
  font-size: 13px;
  color: var(--ion-color-danger);
}
</style>