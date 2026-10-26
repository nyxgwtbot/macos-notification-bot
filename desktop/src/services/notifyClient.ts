/**
 * Typed renderer API client for `python3 bot.py api` (notifybot/api.py).
 *
 * Transport: when running under Electron the request goes through the
 * `window.notifyAPI.api.request` bridge (executed in the main process,
 * mirroring hydranote's web:fetch preload/IPC pattern); otherwise it falls
 * back to a direct fetch against the API base URL for vite browser dev.
 * Non-2xx responses are mapped to a thrown ApiError carrying the server
 * `error` text and optional `details[]` list.
 */

import type {
  ApiErrorBody,
  BotConfig,
  EventsResponse,
  FinanceReportResult,
  FinanceReportStatus,
  FinanceSummary,
  FinanceTransactionsResponse,
  HealthResponse,
  TestNotifyResponse,
} from '@/types/bot';
import type { NotifyAPI } from '@/notify';

export const DEFAULT_API_BASE_URL = 'http://127.0.0.1:4769';

export const EVENT_KINDS = ['all', 'notification', 'startup'] as const;
export type EventKind = typeof EVENT_KINDS[number];

export interface EventQueryOptions {
  limit?: number;
  since?: number;
  kind?: EventKind;
}

/** Error thrown by any NotifyClient request: server text + details[] when present. */
export class ApiError extends Error {
  readonly status: number;
  readonly details: string[];

  constructor(message: string, status: number, details: string[] = []) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.details = details;
  }
}

interface BridgeResult {
  success: boolean;
  status?: number | null;
  body?: string | null;
  error?: string | null;
}

function getBridge(): NotifyAPI | undefined {
  if (typeof window === 'undefined') return undefined;
  return window.notifyAPI;
}

function buildQuery(options: EventQueryOptions): string {
  const params: string[] = [];
  if (options.limit !== undefined) params.push(`limit=${encodeURIComponent(String(options.limit))}`);
  if (options.since !== undefined) params.push(`since=${encodeURIComponent(String(options.since))}`);
  if (options.kind !== undefined) params.push(`kind=${encodeURIComponent(options.kind)}`);
  return params.length > 0 ? `?${params.join('&')}` : '';
}

async function fetchBridgeResult(url: string, method: 'GET' | 'POST', body?: unknown): Promise<BridgeResult> {
  try {
    const headers = body === undefined ? undefined : { 'Content-Type': 'application/json' };
    const response = await fetch(url, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body)
    });
    const text = await response.text();
    return { success: true, status: response.status, body: text };
  } catch (err) {
    return { success: false, error: err instanceof Error ? err.message : String(err) };
  }
}

export class NotifyClient {
  readonly baseUrl: string;

  constructor(baseUrl: string = DEFAULT_API_BASE_URL) {
    this.baseUrl = baseUrl.replace(/\/+$/, '');
  }

  async request<T>(method: 'GET' | 'POST', path: string, body?: unknown): Promise<T> {
    const bridge = getBridge()?.api;
    let result: BridgeResult;
    if (bridge && typeof bridge.request === 'function') {
      result = await bridge.request(method, path, body);
    } else {
      result = await fetchBridgeResult(`${this.baseUrl}${path}`, method, body);
    }

    if (!result.success) {
      throw new ApiError(result.error || 'Falha na requisição ao bot API', 0);
    }
    const status = result.status ?? 0;
    let payload: unknown = null;
    if (typeof result.body === 'string' && result.body.trim() !== '') {
      try {
        payload = JSON.parse(result.body);
      } catch {
        throw new ApiError('Resposta do bot API não é JSON válido', status);
      }
    }
    if (status >= 400) {
      const errBody = (payload ?? {}) as ApiErrorBody;
      throw new ApiError(
        errBody.error || `HTTP ${status} do bot API`,
        status,
        Array.isArray(errBody.details) ? errBody.details : []
      );
    }
    return payload as T;
  }

  // #1
  getHealth(): Promise<HealthResponse> {
    return this.request('GET', '/api/health');
  }

  // #2
  getEvents(options: EventQueryOptions = {}): Promise<EventsResponse> {
    return this.request('GET', `/api/events${buildQuery(options)}`);
  }

  // #3
  getConfig(): Promise<BotConfig> {
    return this.request('GET', '/api/config');
  }

  // #4
  saveConfig(config: BotConfig): Promise<{ ok: boolean }> {
    return this.request('POST', '/api/config', config);
  }

  // #5
  getFinanceSummary(month: string): Promise<FinanceSummary> {
    return this.request('GET', `/api/finance/summary?month=${encodeURIComponent(month)}`);
  }

  // #6
  getFinanceTransactions(month: string): Promise<FinanceTransactionsResponse> {
    return this.request('GET', `/api/finance/transactions?month=${encodeURIComponent(month)}`);
  }

  // #7
  getFinanceReport(month: string): Promise<FinanceReportStatus> {
    return this.request('GET', `/api/finance/report?month=${encodeURIComponent(month)}`);
  }

  // #8
  buildFinanceReport(month?: string): Promise<FinanceReportResult> {
    return this.request('POST', '/api/finance/report', month === undefined ? {} : { month });
  }

  // #9
  postTestNotify(): Promise<TestNotifyResponse> {
    return this.request('POST', '/api/test-notify', {});
  }

  /**
   * Convenience for typed event lists: GET /api/events with an incremental
   * `since` cursor (Dashboard live feed).
   */
  async getEventsSince(since: number | null, options: Omit<EventQueryOptions, 'since'> = {}): Promise<EventsResponse> {
    return this.getEvents({ ...options, since: since === null ? undefined : since });
  }
}

export const notifyClient = new NotifyClient();

/** Extract a short, user-friendly message from any thrown error. */
export function errorText(err: unknown): string {
  if (err instanceof ApiError) return err.message;
  if (err instanceof Error) return err.message;
  return String(err);
}