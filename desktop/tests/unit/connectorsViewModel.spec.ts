import { describe, expect, test } from 'vitest';

import type { ConnectorsResponse, ConnectorRow } from '../../src/types/bot';
import {
  PID_RESTART_HINT,
  buttonLabel,
  connectorDisplayName,
  initialConnectorsState,
  isProbing,
  reduceConnectors,
  rowsFromResponse,
  statusChipTone,
  statusChipLabel,
  type ConnectorRowView,
  type ConnectorsState,
} from '../../src/viewModels/connectorsViewModel';

function row(overrides: Partial<ConnectorRow> = {}): ConnectorRow {
  return { status: 'unknown', granted: false, last_probe: null, hint: 'hint text', ...overrides };
}

function response(overrides: Record<string, ConnectorRow> = {}): ConnectorsResponse {
  return {
    pid: 4242,
    connectors: {
      reminders: overrides.reminders ?? row(),
      notes: overrides.notes ?? row(),
      calendar: overrides.calendar ?? row(),
      music: overrides.music ?? row(),
      shortcuts:
        overrides.shortcuts ??
        row({
          status: 'granted',
          granted: true,
          hint: 'Runs through the system CLI — no consent needed.',
        }),
      mail: overrides.mail ?? row(),
      contacts: overrides.contacts ?? row(),
      messages: overrides.messages ?? row(),
      finder: overrides.finder ?? row(),
      quicktime: overrides.quicktime ?? row(),
      system:
        overrides.system ?? row({ status: 'granted', granted: true, hint: 'No consent required.' }),
    },
  };
}

describe('connectors view-model — rows', () => {
  test('flattens the GET payload into display-ordered rows', () => {
    const rows = rowsFromResponse(
      response({ notes: row({ status: 'granted', granted: true, last_probe: 'st' }) })
    );
    expect(rows.map((r) => r.domain)).toEqual([
      'reminders',
      'notes',
      'calendar',
      'music',
      'shortcuts',
      'mail',
      'contacts',
      'messages',
      'finder',
      'quicktime',
      'system',
    ]);
    const notes = rows[1];
    expect(notes.status).toBe('granted');
    expect(notes.granted).toBe(true);
    expect(notes.last_probe).toBe('st');
    expect(notes.group).toBe('native');
    const shortcuts = rows[4];
    expect(shortcuts.name).toBe('Shortcuts');
    expect(shortcuts.group).toBe('always');
    const system = rows[rows.length - 1];
    expect(system.hint).toBe('No consent required.');
  });

  test('unknown API domains degrade to the Other group instead of vanishing', () => {
    const payload = {
      pid: 4242,
      connectors: { ...response().connectors, future_thing: row() },
    } as ConnectorsResponse;
    const rows = rowsFromResponse(payload);
    expect(rows[rows.length - 1]).toMatchObject({
      domain: 'future_thing',
      name: 'future_thing',
      group: 'other',
    });
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
    expect(state.rows.length).toBe(11);
    expect(state.apiPid).toBe(4242);
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

  test('failure flow: keeps probing false and surfaces a probe error (not a load error)', () => {
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
    // F1: probe failures never set the load-error field (the grid stays up)
    expect(state.error).toBeNull();
    expect(state.probeError).toBe('a probe for reminders is already running');
    expect(state.rows[0].status).toBe('unknown');
  });

  test('probe failure holds the wizard session', () => {
    let state: ConnectorsState = reduceConnectors(initialConnectorsState(), {
      type: 'rows-loaded',
      response: response(),
    });
    state = reduceConnectors(state, { type: 'activated', domain: 'reminders' });
    state = reduceConnectors(state, { type: 'enqueued', domain: 'notes' });
    state = reduceConnectors(state, { type: 'probe-start', domain: 'reminders' });
    state = reduceConnectors(state, {
      type: 'probe-failed',
      domain: 'reminders',
      error: 'network down',
    });
    expect(state.active).toBe('reminders');
    expect(state.queue).toEqual(['notes']);
    expect(state.probeError).toBe('network down');
    expect(state.error).toBeNull();
  });

  test('a fresh probe clears a stale probe error', () => {
    let state: ConnectorsState = reduceConnectors(initialConnectorsState(), {
      type: 'activated',
      domain: 'reminders',
    });
    state = reduceConnectors(state, { type: 'probe-start', domain: 'reminders' });
    state = reduceConnectors(state, {
      type: 'probe-failed',
      domain: 'reminders',
      error: 'boom',
    });
    expect(state.probeError).toBe('boom');
    state = reduceConnectors(state, { type: 'activated', domain: 'reminders' });
    expect(state.probeError).toBeNull();
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

  test('card-wide guard: a second probe-start while another probe is in flight is a no-op', () => {
    let state: ConnectorsState = reduceConnectors(initialConnectorsState(), {
      type: 'rows-loaded',
      response: response(),
    });
    state = reduceConnectors(state, { type: 'probe-start', domain: 'notes' });
    const busy = state;
    state = reduceConnectors(state, { type: 'probe-start', domain: 'reminders' });
    expect(state).toBe(busy);
    expect(isProbing(state, 'notes')).toBe(true);
    expect(isProbing(state, 'reminders')).toBe(false);
  });
});

describe('connectors view-model — wizard queue', () => {
  test('enqueued appends a waiting domain and dedupes', () => {
    let state: ConnectorsState = reduceConnectors(initialConnectorsState(), {
      type: 'activated',
      domain: 'reminders',
    });
    state = reduceConnectors(state, { type: 'enqueued', domain: 'notes' });
    expect(state.queue).toEqual(['notes']);
    const withNotes = state;
    state = reduceConnectors(state, { type: 'enqueued', domain: 'notes' });
    expect(state).toBe(withNotes); // duplicate is a no-op
    state = reduceConnectors(state, { type: 'enqueued', domain: 'reminders' });
    expect(state).toBe(withNotes); // active domain is never enqueued
    expect(state.queue).toEqual(['notes']);
  });

  test('queue-advanced moves the head to active and drains', () => {
    let state: ConnectorsState = reduceConnectors(initialConnectorsState(), {
      type: 'activated',
      domain: 'reminders',
    });
    state = reduceConnectors(state, { type: 'enqueued', domain: 'notes' });
    state = reduceConnectors(state, { type: 'enqueued', domain: 'mail' });
    expect(state.active).toBe('reminders');
    expect(state.queue).toEqual(['notes', 'mail']);
    state = reduceConnectors(state, { type: 'queue-advanced' });
    expect(state.active).toBe('notes');
    expect(state.queue).toEqual(['mail']);
    state = reduceConnectors(state, { type: 'queue-advanced' });
    expect(state.active).toBe('mail');
    expect(state.queue).toEqual([]);
    state = reduceConnectors(state, { type: 'queue-advanced' });
    expect(state.active).toBeNull();
  });

  test('queue-cleared resets the wizard session', () => {
    let state: ConnectorsState = reduceConnectors(initialConnectorsState(), {
      type: 'rows-loaded',
      response: response(),
    });
    state = reduceConnectors(state, { type: 'activated', domain: 'reminders' });
    state = reduceConnectors(state, { type: 'enqueued', domain: 'notes' });
    // grant reminders, then simulate a restart during a denied probe
    state = reduceConnectors(state, {
      type: 'probe-succeeded',
      domain: 'reminders',
      row: row({ status: 'granted', granted: true }),
      pid: 9999,
    });
    state = reduceConnectors(state, {
      type: 'probe-succeeded',
      domain: 'notes',
      row: row({ status: 'denied', granted: false }),
      pid: 4242,
    });
    expect(state.grantedNames).toEqual(['Reminders']);
    expect(state.pidRestart).toBe(true);
    state = reduceConnectors(state, { type: 'queue-cleared' });
    expect(state.active).toBeNull();
    expect(state.queue).toEqual([]);
    expect(state.grantedNames).toEqual([]);
    expect(state.pidRestart).toBe(false);
  });

  test('non-granted result holds the queue (no cascade)', () => {
    let state: ConnectorsState = reduceConnectors(initialConnectorsState(), {
      type: 'rows-loaded',
      response: response(),
    });
    state = reduceConnectors(state, { type: 'activated', domain: 'reminders' });
    state = reduceConnectors(state, { type: 'enqueued', domain: 'notes' });
    state = reduceConnectors(state, {
      type: 'probe-succeeded',
      domain: 'reminders',
      row: row({ status: 'denied', granted: false }),
    });
    expect(state.active).toBe('reminders');
    expect(state.queue).toEqual(['notes']);
    expect(state.grantedNames).toEqual([]);
  });

  test('granted result records the display name for the done banner', () => {
    let state: ConnectorsState = reduceConnectors(initialConnectorsState(), {
      type: 'rows-loaded',
      response: response(),
    });
    state = reduceConnectors(state, { type: 'activated', domain: 'reminders' });
    state = reduceConnectors(state, {
      type: 'probe-succeeded',
      domain: 'reminders',
      row: row({ status: 'granted', granted: true }),
      pid: 4242,
    });
    expect(state.grantedNames).toEqual([connectorDisplayName('reminders')]);
    expect(state.grantedNames).toEqual(['Reminders']);
    expect(state.pidRestart).toBe(false);
  });
});

describe('connectors view-model — hints and pid', () => {
  test('hint coalesce rejects an empty probe hint', () => {
    let state: ConnectorsState = reduceConnectors(initialConnectorsState(), {
      type: 'rows-loaded',
      response: response({ reminders: row({ hint: 'prior hint' }) }),
    });
    state = reduceConnectors(state, {
      type: 'probe-succeeded',
      domain: 'reminders',
      row: row({ hint: '' }),
      pid: 4242,
    });
    expect(state.rows[0].hint).toBe('prior hint');
  });

  test('fresh probe hint wins over the stale one', () => {
    let state: ConnectorsState = reduceConnectors(initialConnectorsState(), {
      type: 'rows-loaded',
      response: response({ reminders: row({ hint: 'prior hint' }) }),
    });
    state = reduceConnectors(state, {
      type: 'probe-succeeded',
      domain: 'reminders',
      row: row({ hint: 'fresh hint' }),
      pid: 4242,
    });
    expect(state.rows[0].hint).toBe('fresh hint');
  });

  test('probe-succeeded records apiPid even without a restart', () => {
    let state: ConnectorsState = reduceConnectors(initialConnectorsState(), {
      type: 'rows-loaded',
      response: response(),
    });
    state = reduceConnectors(state, {
      type: 'probe-succeeded',
      domain: 'reminders',
      row: row({ status: 'granted', granted: true, hint: 'granted now' }),
      pid: 9999,
    });
    expect(state.apiPid).toBe(9999);
    expect(state.pidRestart).toBe(false);
    expect(state.rows[0].hint).toBe('granted now');
  });

  test('pid restart with a non-granted result flags the row hint', () => {
    let state: ConnectorsState = reduceConnectors(initialConnectorsState(), {
      type: 'rows-loaded',
      response: response(),
    });
    state = reduceConnectors(state, {
      type: 'probe-succeeded',
      domain: 'reminders',
      row: row({ status: 'denied', granted: false, hint: 'denied hint' }),
      pid: 9999,
    });
    expect(state.pidRestart).toBe(true);
    expect(state.rows[0].hint).toBe(PID_RESTART_HINT);
    expect(state.apiPid).toBe(9999);
  });

  test('a same-domain re-activation clears the restart flag before the repair flight', () => {
    let state: ConnectorsState = reduceConnectors(initialConnectorsState(), {
      type: 'rows-loaded',
      response: response(),
    });
    state = reduceConnectors(state, {
      type: 'probe-succeeded',
      domain: 'reminders',
      row: row({ status: 'denied', granted: false }),
      pid: 9999,
    });
    expect(state.pidRestart).toBe(true);
    state = reduceConnectors(state, { type: 'activated', domain: 'reminders' });
    expect(state.pidRestart).toBe(false);
  });

  test('a pid-less reload preserves the last known apiPid', () => {
    let state: ConnectorsState = reduceConnectors(initialConnectorsState(), {
      type: 'rows-loaded',
      response: response(),
    });
    expect(state.apiPid).toBe(4242);
    const pidless = response();
    delete (pidless as { pid?: number }).pid;
    state = reduceConnectors(state, { type: 'rows-loaded', response: pidless });
    expect(state.apiPid).toBe(4242);
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

  test('buttonLabel maps each status to its action wording', () => {
    const base: Omit<ConnectorRowView, 'status' | 'granted'> = {
      domain: 'notes',
      name: 'Notes',
      group: 'native',
      last_probe: null,
      hint: '',
    };
    const unknownView: ConnectorRowView = { ...base, status: 'unknown', granted: false };
    const consentView: ConnectorRowView = { ...base, status: 'needs_consent', granted: false };
    const deniedView: ConnectorRowView = { ...base, status: 'denied', granted: false };
    const grantedView: ConnectorRowView = { ...base, status: 'granted', granted: true };
    expect(buttonLabel(unknownView)).toBe('Connect');
    expect(buttonLabel(consentView)).toBe('Check again');
    expect(buttonLabel(deniedView)).toBe('Repair');
    expect(buttonLabel(grantedView)).toBe('Re-check');
  });
});
