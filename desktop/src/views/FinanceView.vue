<template>
  <ion-page>
    <ion-header>
      <ion-toolbar class="nb-page-header">
        <ion-title>Finance (legacy)</ion-title>
        <ion-buttons slot="end">
          <ion-note class="nb-legacy-note">
            Pre-agent example — the agent builds finance dashboards in Chat.
          </ion-note>
        </ion-buttons>
      </ion-toolbar>
    </ion-header>

    <ion-content class="nb-content">
      <!-- disabled state -->
      <div v-if="financeDisabled" class="nb-banner nb-banner-warn">
        <ion-icon aria-hidden="true" :icon="lockClosedOutline" />
        <span>Finance plugin is disabled (config.finance.enabled = false). Enable it in Settings.</span>
      </div>

      <!-- error banner -->
      <div v-if="store.financeError" class="nb-banner nb-banner-err">
        <span>{{ store.financeError }}</span>
        <ion-button size="small" fill="outline" @click="reload">Retry</ion-button>
      </div>

      <!-- month navigator -->
      <div class="nb-month-nav">
        <ion-button fill="clear" size="small" :disabled="financeDisabled" aria-label="Previous month" @click="gotoMonth(previousMonth(store.financeMonth))">
          <ion-icon aria-hidden="true" :icon="chevronBackOutline" />
        </ion-button>
        <ion-chip color="primary" :disabled="financeDisabled" @click="gotoMonth(currentMonth())">
          {{ store.financeMonth || currentMonth() }} · {{ monthLabelEn(store.financeMonth || currentMonth()) }}
        </ion-chip>
        <ion-button fill="clear" size="small" :disabled="financeDisabled" aria-label="Next month" @click="gotoMonth(nextMonth(store.financeMonth))">
          <ion-icon aria-hidden="true" :icon="chevronForwardOutline" />
        </ion-button>
      </div>

      <ion-spinner v-if="store.financeLoading && !store.financeSummary" name="crescent" class="nb-center" />

      <template v-if="store.financeSummary">
        <!-- summary cards -->
        <div class="nb-summary-row" :class="{ 'nb-summary-row-disabled': financeDisabled }">
          <ion-card>
            <ion-card-header><ion-card-title>Total this month</ion-card-title></ion-card-header>
            <ion-card-content>
              <div class="nb-total" :class="{ 'nb-total-zero': !store.financeSummary.db_exists }">
                {{ formatBrl(store.financeSummary.total) }}
              </div>
              <div v-if="!store.financeSummary.db_exists" class="nb-muted">No finance.db — summary is zeroed.</div>
            </ion-card-content>
          </ion-card>
          <ion-card>
            <ion-card-header><ion-card-title>Previous month</ion-card-title></ion-card-header>
            <ion-card-content>
              <div class="nb-total nb-total-smaller">{{ formatBrl(store.financeSummary.prev_total) }}</div>
              <div class="nb-mom" :class="momClass">
                <template v-if="momLabel !== null">{{ momLabel }} vs previous month</template>
                <template v-else><span class="nb-muted">no comparison base</span></template>
              </div>
            </ion-card-content>
          </ion-card>
          <ion-card>
            <ion-card-header><ion-card-title>Transactions</ion-card-title></ion-card-header>
            <ion-card-content>
              <div class="nb-total nb-total-smaller">{{ store.financeSummary.count }}</div>
              <div class="nb-muted" v-if="byKind">income: {{ byKind.recebimento ? formatBrl(byKind.recebimento) : '—' }}</div>
            </ion-card-content>
          </ion-card>
        </div>

        <!-- by-category inline bars (no chart lib) -->
        <ion-card v-if="categoryBars.length > 0">
          <ion-card-header><ion-card-title>By category</ion-card-title></ion-card-header>
          <ion-card-content>
            <div class="nb-cat-list">
              <div v-for="bar in categoryBars" :key="bar.category" class="nb-cat-bar">
                <span class="nb-cat-label">{{ bar.category }}</span>
                <span class="nb-cat-track"><span class="nb-cat-fill" :style="{ width: bar.width + '%' }" /></span>
                <span class="nb-cat-value">{{ formatBrl(bar.value) }}</span>
              </div>
            </div>
          </ion-card-content>
        </ion-card>

        <!-- ledger table -->
        <ion-card>
          <ion-card-header><ion-card-title>Ledger</ion-card-title></ion-card-header>
          <ion-card-content class="nb-ledger-wrap">
            <p v-if="store.financeTransactions.length === 0" class="nb-empty">No transactions recorded for this month.</p>
            <table v-else class="nb-ledger">
              <thead>
                <tr><th>Date</th><th>Type</th><th>Category</th><th class="nb-num">Amount</th><th>Description</th><th>App</th></tr>
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
          <ion-card-header><ion-card-title>Report</ion-card-title></ion-card-header>
          <ion-card-content>
            <div class="nb-report-row">
              <ion-button fill="solid" :disabled="store.reportBusy || financeDisabled" @click="generate">
                <ion-spinner v-if="store.reportBusy" name="crescent" />
                <template v-else>{{ store.reportStatus?.exists ? '(Re)generate report' : 'Generate report' }}</template>
              </ion-button>
              <template v-if="reportPath">
                <ion-button fill="outline" :disabled="!reportOpenable" @click="openReport">Open in browser</ion-button>
                <ion-button fill="outline" :disabled="!reportOpenable" @click="revealReport">Reveal in Finder</ion-button>
              </template>
            </div>
            <div v-if="reportPath" class="nb-report-path">
              {{ reportPath }} {{ store.reportStatus?.exists && !store.reportResult ? '(already exists — click (Re)generate to refresh)' : '' }}
            </div>
            <div v-if="store.reportResult" class="nb-report-ok">
              <strong>Report generated:</strong> {{ store.reportResult.html_path }}
            </div>
            <!-- R6: clamped to 2 lines (shared .nb-error-line); full text in title -->
            <div v-if="store.reportError" class="nb-error-line" :title="store.reportError">{{ store.reportError }}</div>
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
  IonButtons,
  IonCard,
  IonCardContent,
  IonCardHeader,
  IonCardTitle,
  IonChip,
  IonContent,
  IonHeader,
  IonIcon,
  IonNote,
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
  nextMonth,
  previousMonth
} from '@/utils/financeFormat';
import { formatIsoShort } from '@/utils/time';

const store = useBotStore();

const monthFormatter = new Intl.DateTimeFormat('en-US', { month: 'long', year: 'numeric' });

function monthLabelEn(monthStr: string): string {
  const [y, m] = monthStr.split('-').map(Number);
  if (!y || !m) return monthStr;
  const date = new Date(Date.UTC(y, m - 1, 15));
  return monthFormatter.format(date);
}

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
  if (!window.notifyAPI?.report) return false; // browser dev: do not open local files
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
    if (!result.success) console.warn('openExternal failed:', result.error);
  });
}

function revealReport() {
  const bridge = window.notifyAPI?.report;
  if (!bridge || !reportPath.value) return;
  void bridge.revealInFinder(reportPath.value).then((result) => {
    if (!result.success) console.warn('revealInFinder failed:', result.error);
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
.nb-legacy-note {
  font-size: var(--nb-fs-secondary);
  color: var(--nb-text-secondary);
  margin-inline-end: var(--nb-space-3);
}
.nb-month-nav {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: var(--nb-space-2);
  margin-top: var(--nb-space-4);
  margin-bottom: var(--nb-space-4);
}
.nb-summary-row {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
  gap: var(--nb-space-3);
  margin-bottom: 0;
}
.nb-summary-row-disabled {
  opacity: 0.6;
}
.nb-total {
  font-size: var(--nb-fs-display);
  font-weight: 700;
  line-height: 1.2;
  color: var(--ion-color-primary);
}
.nb-total-smaller {
  font-size: var(--nb-fs-title);
  font-weight: 600;
  line-height: 1.3;
}
.nb-total-zero {
  color: var(--nb-text-secondary);
}
.nb-mom {
  font-size: var(--nb-fs-secondary);
}
.nb-mom-up {
  color: var(--ion-color-danger);
}
.nb-mom-down {
  color: var(--ion-color-success);
}
.nb-cat-list {
  display: flex;
  flex-direction: column;
  gap: var(--nb-space-2);
}
.nb-cat-bar {
  display: grid;
  grid-template-columns: 130px 1fr 90px;
  align-items: center;
  gap: var(--nb-space-2);
  margin-bottom: 0;
  font-size: var(--nb-fs-secondary);
}
.nb-cat-track {
  display: block;
  height: 12px;
  border-radius: var(--nb-radius-sm);
  background: var(--nb-bg-elevated);
  overflow: hidden;
}
.nb-cat-fill {
  display: block;
  height: 100%;
  border-radius: var(--nb-radius-sm);
  background: var(--ion-color-primary);
}
.nb-cat-value {
  text-align: right;
}
.nb-ledger {
  width: 100%;
  border-collapse: collapse;
  font-size: var(--nb-fs-body);
  font-family: var(--nb-mono);
}
.nb-ledger th,
.nb-ledger td {
  text-align: left;
  padding: var(--nb-space-1) var(--nb-space-2);
  border-bottom: 1px solid var(--nb-border-subtle);
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
  gap: var(--nb-space-2);
  align-items: center;
}
.nb-report-path {
  margin-top: var(--nb-space-2);
  font-size: var(--nb-fs-micro);
  color: var(--nb-text-secondary);
  font-family: var(--nb-mono);
  word-break: break-all;
}
.nb-report-ok {
  margin-top: var(--nb-space-1);
  font-size: var(--nb-fs-secondary);
  color: var(--ion-color-success);
}
</style>
