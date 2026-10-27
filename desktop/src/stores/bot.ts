/**
 * Pinia store for the bot desktop UI: status, events ring, finance cache,
 * config draft and per-section last errors (meshNest src/stores/ style:
 * one domain store, views poll via composables).
 */

import { defineStore } from 'pinia';

import { notifyClient, errorText, type EventKind } from '@/services/notifyClient';
import type { NotifyDaemonStatus } from '@/notify';
import { eventToRow, eventKey, type EventRow } from '@/viewModels/eventViewModel';
import type {
  BotConfig,
  BotEvent,
  FinanceReportResult,
  FinanceReportStatus,
  FinanceSummary,
  FinanceTransaction,
  HealthResponse,
} from '@/types/bot';

const EVENTS_RING_MAX = 500;
const DAEMON_UNAVAILABLE = 'bridge-ausente';

export interface LogLineItem {
  line_no: number;
  raw: string;
  malformed: boolean;
}

interface BotState {
  // health (GET /api/health, low frequency)
  health: HealthResponse | null;
  healthError: string | null;
  healthLoading: boolean;
  // live feed (GET /api/events incremental)
  events: EventRow[];
  feedError: string | null;
  feedLoading: boolean;
  sinceCursor: number | null;
  lastFeedSuccessAt: string | null;
  /** Live feed kind filter ('all' = no server-side kind param). */
  feedKind: EventKind;
  // daemon process (Electron bridge only)
  daemon: NotifyDaemonStatus | null;
  daemonError: string | null;
  daemonBusy: boolean;
  // finance (GET per month)
  financeMonth: string;
  financeSummary: FinanceSummary | null;
  financeTransactions: FinanceTransaction[];
  financeError: string | null;
  financeLoading: boolean;
  reportStatus: FinanceReportStatus | null;
  reportBusy: boolean;
  reportResult: FinanceReportResult | null;
  reportError: string | null;
  // config
  config: BotConfig | null;
  configError: string | null;
  configLoading: boolean;
  configSaving: boolean;
  configSaveErrors: string[];
  lastSavedAt: string | null;
  // logs
  logLines: LogLineItem[];
  logFileExists: boolean;
  logsError: string | null;
}

export const useBotStore = defineStore('bot', {
  state: (): BotState => ({
    health: null,
    healthError: null,
    healthLoading: false,
    events: [],
    feedError: null,
    feedLoading: false,
    sinceCursor: null,
    lastFeedSuccessAt: null,
    feedKind: 'all',
    daemon: null,
    daemonError: null,
    daemonBusy: false,
    financeMonth: '',
    financeSummary: null,
    financeTransactions: [],
    financeError: null,
    financeLoading: false,
    reportStatus: null,
    reportBusy: false,
    reportResult: null,
    reportError: null,
    config: null,
    configError: null,
    configLoading: false,
    configSaving: false,
    configSaveErrors: [],
    lastSavedAt: null,
    logLines: [],
    logFileExists: false,
    logsError: null
  }),

  getters: {
    /** True only after (at least) one successful feed poll. */
    apiStarted(state): boolean {
      return state.lastFeedSuccessAt !== null;
    },
    /** Last feed poll failure -> offline banner (retry re-polls). */
    apiDown(state): boolean {
      return state.feedError !== null;
    }
  },

  actions: {
    resetFeed() {
      this.events = [];
      this.sinceCursor = null;
      this.feedError = null;
    },

    /** GET /api/health (also seeds finance.enabled gating in views). */
    async loadHealth() {
      this.healthLoading = true;
      try {
        this.health = await notifyClient.getHealth();
        this.healthError = null;
      } catch (err) {
        this.healthError = errorText(err);
      } finally {
        this.healthLoading = false;
      }
    },

    /** Incremental feed poll: `since` cursor + dedupe by event key. The kind
     * filter travels with every poll so the 2s loop keeps it applied. */
    async pollFeed(limit = 100) {
      try {
        const options: { since?: number; limit: number; kind?: EventKind } = {
          limit,
          since: this.sinceCursor ?? undefined
        };
        if (this.feedKind !== 'all') options.kind = this.feedKind;
        const response = await notifyClient.getEvents(options);
        for (const raw of response.events) {
          const event: BotEvent = raw;
          this.appendToFeed(event);
        }
        // Cursor advances by the newest rec_id seen (startup events pass the
        // server filter and are deduped client-side by eventKey).
        const notificationLatest = response.events
          .filter((e: BotEvent) => typeof e.rec_id === 'number')
          .map((e: BotEvent) => e.rec_id as number)
          .reduce<number | null>((acc, n) => (acc === null || n > acc ? n : acc), null);
        this.sinceCursor = notificationLatest ?? this.sinceCursor;
        this.feedError = null;
        this.lastFeedSuccessAt = new Date().toISOString();
      } catch (err) {
        this.feedError = errorText(err);
      }
    },

    appendToFeed(event: BotEvent) {
      const row = eventToRow(event);
      const key = eventKey(event);
      if (this.events.some((existing) => existing.key === key)) return;
      this.events.push(row);
      if (this.events.length > EVENTS_RING_MAX) {
        this.events.splice(0, this.events.length - EVENTS_RING_MAX);
      }
    },

    /** Feed kind filter change: full reload (resetFeed clears the ring and the
     * `since` cursor), then one immediate poll — the 2s poller keeps running. */
    async setFeedKind(kind: EventKind) {
      if (this.feedKind === kind) return;
      this.feedKind = kind;
      this.resetFeed();
      await this.pollFeed();
    },

    /** Daemon process status via the Electron bridge (null in browser dev). */
    async pollDaemon() {
      const bridge = typeof window !== 'undefined' ? window.notifyAPI : undefined;
      if (!bridge?.daemon) {
        this.daemonError = DAEMON_UNAVAILABLE;
        this.daemon = null;
        return;
      }
      try {
        this.daemon = await bridge.daemon.status();
        this.daemonError = null;
      } catch (err) {
        this.daemonError = err instanceof Error ? err.message : String(err);
      }
    },

    async restartDaemon() {
      const bridge = typeof window !== 'undefined' ? window.notifyAPI : undefined;
      if (!bridge?.daemon) {
        this.daemonError = DAEMON_UNAVAILABLE;
        return null;
      }
      this.daemonBusy = true;
      try {
        this.daemon = await bridge.daemon.restart();
        this.daemonError = null;
        return this.daemon;
      } catch (err) {
        this.daemonError = err instanceof Error ? err.message : String(err);
        return null;
      } finally {
        this.daemonBusy = false;
      }
    },

    /** GET /api/finance/summary + /transactions for a month (and report status). */
    async loadFinance(month: string) {
      this.financeMonth = month;
      this.financeLoading = true;
      this.financeError = null;
      this.reportStatus = null;
      const results = await Promise.allSettled([
        notifyClient.getFinanceSummary(month),
        notifyClient.getFinanceTransactions(month),
        notifyClient.getFinanceReport(month)
      ]);
      this.financeSummary = results[0].status === 'fulfilled' ? results[0].value : this.financeSummary;
      this.financeTransactions =
        results[1].status === 'fulfilled' ? results[1].value.records : this.financeTransactions;
      this.reportStatus = results[2].status === 'fulfilled' ? results[2].value : null;
      const firstRejected = results.find((result) => result.status === 'rejected');
      if (firstRejected && firstRejected.status === 'rejected') {
        this.financeError = errorText(firstRejected.reason);
      } else {
        this.financeError = null;
      }
      this.financeLoading = false;
    },

    /** POST /api/finance/report (LLM report build; slow — spinner state). */
    async buildReport(month: string) {
      this.reportBusy = true;
      this.reportError = null;
      this.reportResult = null;
      try {
        const result = await notifyClient.buildFinanceReport(month);
        this.reportResult = result;
        this.reportStatus = { month: result.month, html_path: result.html_path, exists: true };
        return result;
      } catch (err) {
        this.reportError = errorText(err);
        return null;
      } finally {
        this.reportBusy = false;
      }
    },

    /** GET /api/config (masked). */
    async loadConfig() {
      this.configLoading = true;
      this.configError = null;
      try {
        this.config = await notifyClient.getConfig();
        this.configSaveErrors = [];
      } catch (err) {
        this.configError = errorText(err);
      } finally {
        this.configLoading = false;
      }
    },

    /** POST /api/config with client-side validation; returns true on success. */
    async saveConfig(config: BotConfig) {
      this.configSaving = true;
      this.configSaveErrors = [];
      this.configError = null;
      try {
        await notifyClient.saveConfig(config);
        this.config = await notifyClient.getConfig();
        this.lastSavedAt = new Date().toISOString();
        return true;
      } catch (err) {
        this.configError = errorText(err);
        this.configSaveErrors = (err as { details?: string[] }).details ?? [];
        return false;
      } finally {
        this.configSaving = false;
      }
    },

    /** GET /api/logs/tail (raw lines with malformed flags). */
    async pollLogs(lines = 300) {
      try {
        const response = await notifyClient.request<{
          path: string;
          exists: boolean;
          lines: LogLineItem[];
          count: number;
        }>('GET', `/api/logs/tail?lines=${lines}`);
        this.logLines = response.lines;
        this.logFileExists = response.exists;
        this.logsError = null;
      } catch (err) {
        this.logsError = errorText(err);
      }
    }
  }
});