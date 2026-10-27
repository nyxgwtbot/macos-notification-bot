import { describe, expect, test, afterEach, vi } from 'vitest';

import {
  chipsFromRecords,
  iframeUrlFrom,
  initialChatState,
  isIpcTimeout,
  reduceChat,
  toToolChip,
  type ChatState
} from '../../src/viewModels/chatViewModel';
import { NotifyClient } from '../../src/services/notifyClient';
import type { NotifyAPI } from '../../src/notify';
import type { AgentRecord, AgentSessionDetail, AgentTurnResponse } from '../../src/types/bot';

// ---- helpers -------------------------------------------------------------

const HEX32 = 'a'.repeat(32);
const ARTIFACT_URL = `http://127.0.0.1:4770/${HEX32}.html`;

function installBridge(bridge: Partial<NotifyAPI> | null): void {
  if (bridge === null) {
    delete window.notifyAPI;
    return;
  }
  window.notifyAPI = bridge as NotifyAPI;
}

function chainResponse(): AgentTurnResponse {
  return {
    session_id: '11111111-2222-3333-4444-555555555555',
    turn: [
      { role: 'assistant', tool: 'app.data', args: { dataset: 'health' }, result: { ok: true }, ts: 't1' },
      { role: 'assistant', tool: 'dashboard.create', args: { title: 'H' }, result: { artifact_id: HEX32, path: '/x' }, ts: 't2' },
      { role: 'assistant', tool: 'server.serve', args: { artifact_id: HEX32 }, result: { url: ARTIFACT_URL }, ts: 't3' }
    ],
    answer: 'Aqui está o dashboard!'
  , urls: [ARTIFACT_URL] };
}

function withState(partial: Partial<ChatState>): ChatState {
  return { ...initialChatState(), ...partial };
}

// ---- iframe allowlist -----------------------------------------------------

describe('iframeUrlFrom allowlist', () => {
  test('accepts a 4770 hex-id artifact URL', () => {
    expect(iframeUrlFrom([ARTIFACT_URL])).toBe(ARTIFACT_URL);
  });

  test('returns the first valid URL among a mixed list', () => {
    expect(iframeUrlFrom(['http://evil/x.html', 'https://a.b', ARTIFACT_URL])).toBe(ARTIFACT_URL);
  });

  test('rejects foreign hosts, schemes, ports and malformed ids', () => {
    const invalid = [
      'http://evil.example/x.html',
      'https://127.0.0.1:4770/' + HEX32 + '.html',
      'http://127.0.0.1:4771/' + HEX32 + '.html',
      'http://127.0.0.1:4770/not-an-id.html',
      'http://127.0.0.1:4770/' + HEX32 + '.html?x=1',
      'http://127.0.0.1:4770/' + '..'.repeat(8) + '.html',
      'http://localhost:4770/' + HEX32 + '.html'
    ];
    for (const url of invalid) {
      expect(iframeUrlFrom([url])).toBeNull();
    }
  });

  test('handles null/undefined/empty and non-string entries', () => {
    expect(iframeUrlFrom(null)).toBeNull();
    expect(iframeUrlFrom(undefined)).toBeNull();
    expect(iframeUrlFrom([])).toBeNull();
    expect(iframeUrlFrom([123] as unknown as string[])).toBeNull();
  });
});

// ---- tool chips -----------------------------------------------------------

describe('tool chips mapping', () => {
  test('ok chip pretty-prints the result JSON', () => {
    const chip = toToolChip(
      { role: 'assistant', tool: 'app.data', args: {}, result: { ok: true, count: 2 }, ts: 't1' },
      0
    );
    expect(chip.ok).toBe(true);
    expect(chip.tool).toBe('app.data');
    expect(chip.preview).toBe(JSON.stringify({ ok: true, count: 2 }, null, 2));
    expect(chip.id).toContain('t1');
  });

  test('error chip carries the error text and ok=false', () => {
    const chip = toToolChip(
      { role: 'assistant', tool: 'dashboard.create', ts: 't2', error: 'html rejected: pattern "<script"' },
      0
    );
    expect(chip.ok).toBe(false);
    expect(chip.preview).toContain('<script');
  });

  test('preview truncates long results to the cap with a marker', () => {
    const chip = toToolChip(
      { role: 'assistant', tool: 'app.data', ts: 't3', result: { blob: 'x'.repeat(6000) } },
      0
    );
    expect(chip.preview.length).toBeLessThanOrEqual(2000 + '\n…[truncated]'.length);
    expect(chip.preview).toContain('…[truncated]');
  });

  test('chipsFromRecords maps a persisted transcript (answers skipped)', () => {
    const records: AgentRecord[] = [
      { role: 'user', content: 'ping', ts: 'a' },
      { role: 'assistant', tool: 'app.data', args: {}, result: { ok: true }, ts: 'b' },
      { role: 'assistant', tool: 'notify.test', args: {}, error: 'osascript falhou', ts: 'c' },
      { role: 'assistant', answer: 'done', urls: [], ts: 'd' }
    ];
    const chips = chipsFromRecords(records);
    expect(chips.map((c) => [c.tool, c.ok])).toEqual([
      ['app.data', true],
      ['notify.test', false]
    ]);
  });
});

// ---- reduce flow ----------------------------------------------------------

describe('send → succeed flow', () => {
  test('user bubble appears immediately and pending clears on answer', () => {
    let state = initialChatState();
    state = reduceChat(state, { type: 'send', message: 'dashboard, por favor' });
    expect(state.messages).toHaveLength(1);
    expect(state.messages[0]).toMatchObject({ role: 'user', text: 'dashboard, por favor' });
    expect(state.pending).toBe(true);
    expect(state.error).toBeNull();

    state = reduceChat(state, { type: 'turn-succeeded', response: chainResponse() });
    expect(state.pending).toBe(false);
    expect(state.messages).toHaveLength(2);
    expect(state.messages[1]).toMatchObject({ role: 'assistant', text: 'Aqui está o dashboard!' });
    expect(state.sessionId).toBe('11111111-2222-3333-4444-555555555555');
    expect(state.chips).toHaveLength(3);
    expect(state.artifactUrl).toBe(ARTIFACT_URL);
  });

  test('turn answer without urls keeps the previous dashboard visible', () => {
    const state0 = withState({ artifactUrl: ARTIFACT_URL, sessionId: 'sid' });
    const state1 = reduceChat(state0, {
      type: 'turn-succeeded',
      response: { session_id: 'sid', turn: [], answer: 'pronto', urls: [] }
    });
    expect(state1.artifactUrl).toBe(ARTIFACT_URL);
  });

  test('turn failure sets the error line; error-cleared hides it', () => {
    let state = reduceChat(initialChatState(), { type: 'send', message: 'ping' });
    state = reduceChat(state, { type: 'turn-failed', error: 'ECONNREFUSED' });
    expect(state.pending).toBe(false);
    expect(state.error).toBe('ECONNREFUSED');
    state = reduceChat(state, { type: 'error-cleared' });
    expect(state.error).toBeNull();
  });
});

// ---- sessions -------------------------------------------------------------

describe('session selection and reset', () => {
  test('session-selected(null) resets state but keeps the sessions list', () => {
    const state0 = withState({
      sessionId: 'sid-a',
      messages: [{ id: 'b1', role: 'user', text: 'x' }],
      chips: [{ id: 'c1', tool: 'app.data', ok: true, preview: '{}' }],
      artifactUrl: ARTIFACT_URL,
      pending: true,
      error: 'boom',
      sessions: [{ session_id: 'sid-a', updated_at: 'u', turns: 1, preview: 'x' }]
    });
    const state1 = reduceChat(state0, { type: 'session-selected', sessionId: null });
    expect(state1).toMatchObject({
      sessionId: null,
      pending: false,
      error: null,
      artifactUrl: null
    });
    expect(state1.messages).toEqual([]);
    expect(state1.chips).toEqual([]);
    expect(state1.sessions).toHaveLength(1); // the picker list survives the reset
  });

  test('sessions-loaded replaces the list', () => {
    const sessions = [{ session_id: 's', updated_at: 'u', turns: 0, preview: 'p' }];
    const state = reduceChat(initialChatState(), { type: 'sessions-loaded', sessions });
    expect(state.sessions).toEqual(sessions);
  });

  test('session-loaded rebuilds bubbles/chips only for the current session', () => {
    const records: AgentRecord[] = [
      { role: 'user', content: 'dashboard?', ts: 'r1' },
      { role: 'assistant', tool: 'app.data', args: {}, result: {}, ts: 'r2' },
      { role: 'assistant', answer: 'feito!', urls: [ARTIFACT_URL], ts: 'r3' }
    ];
    const state0 = withState({ sessionId: 'sid' });
    const state1 = reduceChat(state0, { type: 'session-loaded', sessionId: 'sid', records });
    expect(state1.messages.map((m) => [m.role, m.text])).toEqual([
      ['user', 'dashboard?'],
      ['assistant', 'feito!']
    ]);
    expect(state1.chips).toHaveLength(1);
    expect(state1.artifactUrl).toBe(ARTIFACT_URL);
    expect(state1.error).toBeNull();

    // a stale transcript for a DIFFERENT session is ignored (identity check)
    const stale = reduceChat(state0, { type: 'session-loaded', sessionId: 'other', records });
    expect(stale).toBe(state0);
  });
});

// ---- IPC timeout recovery (bridge-transport rehearsal) ---------------------

describe('IPC-timeout recovery path calls getAgentSession', () => {
  afterEach(() => {
    installBridge(null);
    vi.unstubAllGlobals();
  });

  test('a bridge timeout error is recognized by isIpcTimeout', () => {
    expect(isIpcTimeout(new Error('timeout da chamada ao bot API (150s)'))).toBe(true);
    expect(isIpcTimeout(new Error('ECONNREFUSED'))).toBe(false);
    expect(isIpcTimeout(undefined)).toBe(false);
  });

  test('timed-out POST over the bridge -> getAgentSession -> reduce maps the finished turn', async () => {
    const recoveredDetail: AgentSessionDetail = {
      session_id: 'sid',
      records: [
        { role: 'user', content: 'dashboard?', ts: 'r1' },
        { role: 'assistant', tool: 'server.serve', args: {}, result: { url: ARTIFACT_URL }, ts: 'r2' },
        { role: 'assistant', answer: 'pronto!', urls: [ARTIFACT_URL], ts: 'r3' }
      ]
    };
    const requestMock = vi.fn();
    requestMock
      .mockResolvedValueOnce({
        success: false,
        status: null,
        body: null,
        error: 'timeout da chamada ao bot API (150s)'
      })
      .mockResolvedValueOnce({ success: true, status: 200, body: JSON.stringify(recoveredDetail) });
    installBridge({ api: { request: requestMock as unknown as NotifyAPI['api']['request'] } });

    const client = new NotifyClient();
    let recovered: AgentSessionDetail | null = null;
    let failedWithTimeout = false;
    try {
      await client.postAgentTurn('sid', 'dashboard?');
    } catch (err) {
      failedWithTimeout = isIpcTimeout(err);
      if (failedWithTimeout) {
        recovered = await client.getAgentSession('sid');
      }
    }

    expect(failedWithTimeout).toBe(true);
    expect(requestMock).toHaveBeenNthCalledWith(1, 'POST', '/api/agent/turn', {
      session_id: 'sid',
      message: 'dashboard?'
    });
    expect(requestMock).toHaveBeenNthCalledWith(2, 'GET', '/api/agent/session/sid', undefined);
    expect(recovered?.records).toHaveLength(3);

    // The view's recovery dispatch — the finished turn is fully usable.
    const state = reduceChat(withState({ sessionId: 'sid' }), {
      type: 'session-loaded',
      sessionId: 'sid',
      records: recovered.records
    });
    expect(state.pending).toBe(false);
    expect(state.error).toBeNull();
    expect(state.messages.map((m) => m.text)).toEqual(['dashboard?', 'pronto!']);
    expect(state.artifactUrl).toBe(ARTIFACT_URL);
  });
});