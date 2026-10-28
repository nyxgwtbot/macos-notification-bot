/**
 * Connectors view-model: pure state machine for the Settings › Connectors
 * card (tests: tests/unit/connectorsViewModel.spec.ts). Mirrors
 * chatViewModel.ts conventions: no Ionic imports, pure reduce over typed
 * events, exported mapping helpers.
 */

import type { ConnectorsResponse, ConnectorRow } from '@/types/bot';

/** Domain display order (matches GET /api/connectors row order). */
export const CONNECTOR_DOMAIN_ORDER = ['reminders', 'notes', 'system'] as const;

export interface ConnectorRowView {
  domain: string;
  status: ConnectorRow['status'];
  granted: boolean;
  last_probe: string | null;
  hint: string;
}

export interface ConnectorsState {
  rows: ConnectorRowView[];
  probing: Record<string, boolean>;
  error: string | null;
  loaded: boolean;
}

export type ConnectorsEvent =
  | { type: 'load-start' }
  | { type: 'rows-loaded'; response: ConnectorsResponse }
  | { type: 'load-failed'; error: string }
  | { type: 'probe-start'; domain: string }
  | { type: 'probe-succeeded'; domain: string; row: ConnectorRow }
  | { type: 'probe-failed'; domain: string; error: string };

export function initialConnectorsState(): ConnectorsState {
  return { rows: [], probing: {}, error: null, loaded: false };
}

/** Flatten the GET /api/connectors payload into display-ordered row views. */
export function rowsFromResponse(response: ConnectorsResponse | null | undefined): ConnectorRowView[] {
  if (!response || !response.connectors) return [];
  return CONNECTOR_DOMAIN_ORDER.filter((domain) => response.connectors[domain]).map((domain) => {
    const row = response.connectors[domain];
    return {
      domain,
      status: row.status,
      granted: row.granted === true,
      last_probe: row.last_probe ?? null,
      hint: row.hint ?? '',
    };
  });
}

/**
 * Chip tone class per connector status — the soft tinted chips in
 * theme/nb-shared.css (ok/warn/bad tone pairs; neutral base otherwise).
 */
export function statusChipTone(status: string): string {
  if (status === 'granted') return 'ok';
  if (status === 'needs_consent') return 'warn';
  if (status === 'denied') return 'bad';
  return 'muted';
}

/** Short English chip label per connector status. */
export function statusChipLabel(status: string): string {
  if (status === 'granted') return 'Granted';
  if (status === 'needs_consent') return 'Needs consent';
  if (status === 'denied') return 'Denied';
  return 'Unknown';
}

/** Button label: ungranted domains Connect, granted ones Re-check. */
export function buttonLabel(row: ConnectorRowView): string {
  return row.granted ? 'Re-check' : 'Connect';
}

/** Concurrent-probe guard: a second probe-start for a busy domain is a no-op. */
export function isProbing(state: ConnectorsState, domain: string): boolean {
  return state.probing[domain] === true;
}

export function reduceConnectors(state: ConnectorsState, event: ConnectorsEvent): ConnectorsState {
  switch (event.type) {
    case 'load-start':
      return { ...state, error: null };
    case 'rows-loaded':
      return {
        ...state,
        rows: rowsFromResponse(event.response),
        probing: {},
        error: null,
        loaded: true,
      };
    case 'load-failed':
      return { ...state, error: event.error, loaded: true };
    case 'probe-start': {
      // concurrent-probe guard: ignore a second start on the same domain
      if (isProbing(state, event.domain)) return state;
      return { ...state, probing: { ...state.probing, [event.domain]: true } };
    }
    case 'probe-succeeded': {
      const rows = state.rows.map((row) =>
        row.domain === event.domain
          ? {
              domain: row.domain,
              status: event.row.status,
              granted: event.row.granted === true,
              last_probe: event.row.last_probe ?? null,
              hint: event.row.hint ?? row.hint,
            }
          : row
      );
      return { ...state, rows, probing: { ...state.probing, [event.domain]: false }, error: null };
    }
    case 'probe-failed':
      return {
        ...state,
        probing: { ...state.probing, [event.domain]: false },
        error: event.error,
      };
    default: {
      const exhaustive: never = event;
      return exhaustive;
    }
  }
}