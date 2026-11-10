/**
 * Connectors view-model: pure state machine for the Settings › Connectors
 * card (tests: tests/unit/connectorsViewModel.spec.ts). Mirrors
 * chatViewModel.ts conventions: no Ionic imports, pure reduce over typed
 * events, exported mapping helpers.
 */

import type { ConnectorsResponse, ConnectorRow } from '@/types/bot';

/**
 * Client-side display metadata per connector domain: friendly name + wizard
 * group. `always` = the server forces the row granted with no consent flow
 * (shortcuts CLI, system); `native` = macOS consent applies. A domain missing
 * from this map degrades to the raw id under the `other` group so an
 * API-side domain addition can never vanish from the card.
 */
export interface ConnectorMeta {
  name: string;
  group: 'native' | 'always' | 'other';
}

export const CONNECTOR_META: Record<string, ConnectorMeta> = {
  reminders: { name: 'Reminders', group: 'native' },
  notes: { name: 'Notes', group: 'native' },
  calendar: { name: 'Calendar', group: 'native' },
  music: { name: 'Music', group: 'native' },
  shortcuts: { name: 'Shortcuts', group: 'always' },
  mail: { name: 'Mail', group: 'native' },
  contacts: { name: 'Contacts', group: 'native' },
  messages: { name: 'Messages', group: 'native' },
  finder: { name: 'Finder', group: 'native' },
  quicktime: { name: 'QuickTime', group: 'native' },
  system: { name: 'System', group: 'always' },
};

/** Shared lead-in for the pid-restart diagnostic (banner + row hint). */
export const PID_RESTART_PREFIX =
  'The assistant restarted during this check, which can drop a fresh permission.';

/** Diagnostic row hint when the API process restarted mid-probe (pid changed + non-granted). */
export const PID_RESTART_HINT = PID_RESTART_PREFIX + ' Press Repair to try again.';

export interface ConnectorRowView {
  domain: string;
  name: string;
  group: ConnectorMeta['group'];
  status: ConnectorRow['status'];
  granted: boolean;
  last_probe: string | null;
  hint: string;
}

export interface ConnectorsState {
  rows: ConnectorRowView[];
  probing: Record<string, boolean>;
  error: string | null;
  /** Probe-flight error (additive banner); never hides the row grid. */
  probeError: string | null;
  loaded: boolean;
  /** Wizard: the domain holding the current probe turn (one prompt at a time). */
  active: string | null;
  /** Wizard: domains waiting their turn, in click order. */
  queue: string[];
  /** Display names granted during the current wizard session (done banner). */
  grantedNames: string[];
  /** pid of the API process that produced the last row/probe payload. */
  apiPid: number | null;
  /** pid changed across the last probe and the result was not granted. */
  pidRestart: boolean;
}

export type ConnectorsEvent =
  | { type: 'load-start' }
  | { type: 'rows-loaded'; response: ConnectorsResponse }
  | { type: 'load-failed'; error: string }
  | { type: 'probe-start'; domain: string }
  | { type: 'probe-succeeded'; domain: string; row: ConnectorRow; pid?: number }
  | { type: 'probe-failed'; domain: string; error: string }
  | { type: 'activated'; domain: string }
  | { type: 'enqueued'; domain: string }
  | { type: 'queue-advanced' }
  | { type: 'queue-cleared' };

export function initialConnectorsState(): ConnectorsState {
  return {
    rows: [],
    probing: {},
    error: null,
    probeError: null,
    loaded: false,
    active: null,
    queue: [],
    grantedNames: [],
    apiPid: null,
    pidRestart: false,
  };
}

/**
 * Friendly display name for a domain; unknown ids degrade to the raw id so a
 * new/renamed API domain stays visible instead of crashing the card.
 */
export function connectorDisplayName(domain: string): string {
  return CONNECTOR_META[domain]?.name ?? domain;
}

/**
 * Flatten the GET payload into display rows in EXACT API order (insertion
 * order of the JSON object — Python dicts serialize in registry order).
 * No client-side domain filter: the API is trusted, the UI mirrors it.
 */
export function rowsFromResponse(response: ConnectorsResponse | null | undefined): ConnectorRowView[] {
  if (!response || !response.connectors) return [];
  return Object.entries(response.connectors).map(([domain, row]) => {
    const meta = CONNECTOR_META[domain] ?? { name: domain, group: 'other' as const };
    return {
      domain,
      name: meta.name,
      group: meta.group,
      status: row.status,
      granted: row.granted === true,
      last_probe: row.last_probe ?? null,
      hint: row.hint ?? '',
    };
  });
}

/**
 * Chip tone class per connector status — the soft tinted chips in
 * theme/nb-shared.css (ok/warn/bad tone pairs; neutral `muted` otherwise).
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

/** Button label per status (recovery wording for denied, recheck for consent). */
export function buttonLabel(row: ConnectorRowView): string {
  if (row.status === 'denied') return 'Repair';
  if (row.status === 'needs_consent') return 'Check again';
  return row.granted ? 'Re-check' : 'Connect';
}

/** Per-domain guard: a second probe-start for a busy domain is a no-op. */
export function isProbing(state: ConnectorsState, domain: string): boolean {
  return state.probing[domain] === true;
}

/** Card-wide guard: at most one probe in flight, ever. */
export function isProbingAnywhere(state: ConnectorsState): boolean {
  return Object.values(state.probing).some(Boolean);
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
        // F4: a pid-less reload keeps the last known pid for restart detection
        apiPid: event.response.pid ?? state.apiPid,
        probeError: null,
        loaded: true,
        // fresh server snapshot resets any in-flight wizard session
        active: null,
        queue: [],
        grantedNames: [],
        pidRestart: false,
      };
    case 'load-failed':
      return { ...state, error: event.error, loaded: true };
    case 'probe-start': {
      // concurrent-probe guard (same domain) + card-wide single-flight:
      // never two consent probes in parallel while a wizard turn is live.
      if (isProbing(state, event.domain) || isProbingAnywhere(state)) return state;
      // any new flight supersedes a stale probe error
      return { ...state, probing: { ...state.probing, [event.domain]: true }, probeError: null };
    }
    case 'probe-succeeded': {
      const priorPid = state.apiPid;
      const pidChanged = priorPid !== null && typeof event.pid === 'number' && event.pid !== priorPid;
      const restart = pidChanged && event.row.status !== 'granted';
      const rows = state.rows.map((row) =>
        row.domain === event.domain
          ? {
              domain: row.domain,
              name: row.name,
              group: row.group,
              status: event.row.status,
              granted: event.row.granted === true,
              last_probe: event.row.last_probe ?? null,
              // coalesce rejects '' — a probe without a hint keeps the prior one
              hint: restart ? PID_RESTART_HINT : event.row.hint || row.hint,
            }
          : row
      );
      const name = connectorDisplayName(event.domain);
      const grantedNames =
        event.row.status === 'granted' && !state.grantedNames.includes(name)
          ? [...state.grantedNames, name]
          : state.grantedNames;
      // queue transitions are parent-driven (queue-advanced) so the reducer
      // stays a dumb transition table; a non-granted result holds the queue.
      return {
        ...state,
        rows,
        probing: { ...state.probing, [event.domain]: false },
        error: null,
        probeError: null,
        apiPid: typeof event.pid === 'number' ? event.pid : priorPid,
        pidRestart: restart,
        grantedNames,
      };
    }
    case 'probe-failed':
      // additive probe error only: the row grid and wizard session stay live
      return {
        ...state,
        probing: { ...state.probing, [event.domain]: false },
        probeError: event.error,
      };
    case 'activated':
      // a fresh attempt always clears any stale restart warning (even a
      // same-domain repair re-flight — otherwise the info banner would sit
      // over the very probe it is asking the user to run).
      return { ...state, active: event.domain, pidRestart: false, probeError: null };
    case 'enqueued': {
      if (event.domain === state.active || state.queue.includes(event.domain)) return state;
      return { ...state, queue: [...state.queue, event.domain] };
    }
    case 'queue-advanced': {
      if (state.queue.length === 0) {
        return state.active === null ? state : { ...state, active: null, pidRestart: false };
      }
      return { ...state, active: state.queue[0], queue: state.queue.slice(1), pidRestart: false };
    }
    case 'queue-cleared':
      return { ...state, active: null, queue: [], grantedNames: [], pidRestart: false, probeError: null };
    default: {
      const exhaustive: never = event;
      return exhaustive;
    }
  }
}
