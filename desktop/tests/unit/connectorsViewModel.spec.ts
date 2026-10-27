import { describe, expect, test } from 'vitest';

import type { ConnectorsResponse, ConnectorRow } from '../../src/types/bot';
import {
  buttonLabel,
  initialConnectorsState,
  isProbing,
  reduceConnectors,
  rowsFromResponse,
  statusChipTone,
  statusChipLabel,
  type ConnectorsState,
} from '../../src/viewModels/connectorsViewModel';

function row(overrides: Partial<ConnectorRow> = {}): ConnectorRow {
  return { status: 'unknown', granted: false, last_probe: null, hint: 'hint text', ...overrides };
}

function response(overrides: { reminders?: ConnectorRow; notes?: ConnectorRow; system?: ConnectorRow } = {}): ConnectorsResponse {
  return {
    connectors: {
      reminders: overrides.reminders ?? row(),
      notes: overrides.notes ?? row(),
      system: overrides.system ?? row({ status: 'granted', granted: true, hint: 'no consent required' }),
    }
  };
}

describe('connectors view-model — rows', () => {
  test('flattens the GET payload into display-ordered rows', () => {
    const rows = rowsFromResponse(
      response({ notes: row({ status: 'granted', granted: true, last_probe: 'st' }) })
    );
    expect(rows.map((r) => r.domain)).toEqual(['reminders', 'notes', 'system']);
    const notes = rows[1];
    expect(notes.status).toBe('granted');
    expect(notes.granted).toBe(true);
    expect(notes.last_probe).toBe('st');
    const system = rows[2];
    expect(system.hint).toBe('no consent required');
  });

  test('degrades on a missing/ malformed payload', () => {
    expect(rowsFromResponse(null)).toEqual([]);
    expect(rowsFromResponse(undefined)).toEqual([]);
    expect(rowsFromResponse({} as unknown as ConnectorsResponse)).toEqual([]);
  });

  test('loaded rows replace state and clear the error', () => {
    let state: ConnectorsState = reduceConnectors(initialConnectorsState(), { type: 'load-start' });
    state = reduceConnectors(state, { type: 'load-failed', error: 'boom' });
    expect(state.error).toBe('boom');
    state = reduceConnectors(state, { type: 'rows-loaded', response: response() });
    expect(state.error).toBeNull();
    expect(state.loaded).toBe(true);
    expect(state.rows.length).toBe(3);
  });
});

describe('connectors view-model — probes', () => {
  test('happy flow: start -> succeeded flips the row and clears probing', () => {
    let state: ConnectorsState = reduceConnectors(initialConnectorsState(), {
      type: 'rows-loaded',
      response: response(),
    });
    state = reduceConnectors(state, { type: 'probe-start', domain: 'notes' });
    expect(isProbing(state, 'notes')).toBe(true);
    state = reduceConnectors(state, {
      type: 'probe-succeeded',
      domain: 'notes',
      row: row({ status: 'granted', granted: true, last_probe: 'ts-1' }),
    });
    expect(isProbing(state, 'notes')).toBe(false);
    expect(state.rows[1].status).toBe('granted');
    expect(state.rows[1].granted).toBe(true);
    expect(state.rows[1].last_probe).toBe('ts-1');
    expect(state.rows[0].status).toBe('unknown'); // untouched domain
  });

  test('failure flow: keeps probing false and surfaces the error', () => {
    let state: ConnectorsState = reduceConnectors(initialConnectorsState(), {
      type: 'rows-loaded',
      response: response(),
    });
    state = reduceConnectors(state, { type: 'probe-start', domain: 'reminders' });
    state = reduceConnectors(state, {
      type: 'probe-failed',
      domain: 'reminders',
      error: 'a probe for reminders is already running',
    });
    expect(isProbing(state, 'reminders')).toBe(false);
    expect(state.error).toBe('a probe for reminders is already running');
    expect(state.rows[0].status).toBe('unknown');
  });

  test('concurrent-probe guard: a second start on a busy domain is a no-op', () => {
    let state: ConnectorsState = reduceConnectors(initialConnectorsState(), {
      type: 'rows-loaded',
      response: response(),
    });
    state = reduceConnectors(state, { type: 'probe-start', domain: 'notes' });
    const busy = state;
    state = reduceConnectors(state, { type: 'probe-start', domain: 'notes' });
    expect(state).toBe(busy); // same reference, nothing changed
    expect(isProbing(state, 'notes')).toBe(true);
  });

  test('parallel probes on different domains are independent', () => {
    let state: ConnectorsState = reduceConnectors(initialConnectorsState(), {
      type: 'rows-loaded',
      response: response(),
    });
    state = reduceConnectors(state, { type: 'probe-start', domain: 'notes' });
    state = reduceConnectors(state, { type: 'probe-start', domain: 'reminders' });
    expect(isProbing(state, 'notes')).toBe(true);
    expect(isProbing(state, 'reminders')).toBe(true);
  });
});

describe('connectors view-model — chip mapping', () => {
  test('statusChipTone maps the four statuses to soft-chip tone classes', () => {
    expect(statusChipTone('granted')).toBe('ok');
    expect(statusChipTone('needs_consent')).toBe('warn');
    expect(statusChipTone('denied')).toBe('bad');
    expect(statusChipTone('unknown')).toBe('muted');
    expect(statusChipTone('anything-else')).toBe('muted');
  });

  test('statusChipLabel maps the four statuses to English labels', () => {
    expect(statusChipLabel('granted')).toBe('Granted');
    expect(statusChipLabel('needs_consent')).toBe('Needs consent');
    expect(statusChipLabel('denied')).toBe('Denied');
    expect(statusChipLabel('unknown')).toBe('Unknown');
    expect(statusChipLabel('anything-else')).toBe('Unknown');
  });

  test('buttonLabel flips Connect vs Re-check on granted', () => {
    const grantedView = { domain: 'notes', status: 'granted', granted: true, last_probe: null, hint: '' };
    const unknownView = { domain: 'notes', status: 'unknown', granted: false, last_probe: null, hint: '' };
    expect(buttonLabel(grantedView)).toBe('Re-check');
    expect(buttonLabel(unknownView)).toBe('Connect');
  });
});