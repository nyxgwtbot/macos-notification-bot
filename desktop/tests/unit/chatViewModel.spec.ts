import { describe, expect, test, afterEach, vi } from 'vitest';

import {
  formatBubbleTime,
  iframeUrlFrom,
  initialChatState,
  isIpcTimeout,
  reduceChat,
  toToolChip,
  transcriptFromRecords,
  type ChatState,
  type ChatTranscriptItem
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

/** Compact item outline: bubble -> 'user'/'assistant'; tools -> chips count. */
function outline(items: ChatTranscriptItem[]): Array<string | number> {
  return items.map((item) => (item.kind === 'bubble' ? item.role : item.chips.length));
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
});

// ---- transcript interleave ------------------------------------------------

describe('per-turn tool chips interleave', () => {
  test('send + turn-succeeded: chips group sits under the user bubble, before the answer', () => {
    let state = initialChatState();
    state = reduceChat(state, { type: 'send', message: 'dashboard, por favor' });
    state = reduceChat(state, { type: 'turn-succeeded', response: chainResponse() });
    // [user bubble, tools group (3 chips), assistant bubble]
    expect(outline(state.items)).toEqual(['user', 3, 'assistant']);
    const first = state.items[0];
    const group = state.items[1];
    const answer = state.items[2];
    expect(first.kind === 'bubble' && first.text).toBe('dashboard, por favor');
    expect(group.kind === 'tools' && group.chips.map((c) => [c.tool, c.ok])).toEqual([
      ['app.data', true],
      ['dashboard.create', true],
      ['server.serve', true]
    ]);
    expect(answer.kind === 'bubble' && answer.text).toBe('Aqui está o dashboard!');
    expect(state.artifactUrl).toBe(ARTIFACT_URL);
    expect(state.pending).toBe(false);
  });

  test('tool groups and chip ids are unique across consecutive turns', () => {
    let state = initialChatState();
    state = reduceChat(state, { type: 'send', message: 'primeiro' });
    state = reduceChat(state, { type: 'turn-succeeded', response: chainResponse() });
    state = reduceChat(state, { type: 'send', message: 'de novo' });
    state = reduceChat(state, { type: 'turn-succeeded', response: chainResponse() });
    expect(outline(state.items)).toEqual(['user', 3, 'assistant', 'user', 3, 'assistant']);
    const ids = state.items.map((item) => item.id);
    expect(new Set(ids).size).toBe(ids.length);
    const groupA = state.items[1];
    const groupB = state.items[4];
    expect(groupA.kind === 'tools' && groupB.kind === 'tools' && groupA.chips[0].id).not.toBe(groupB.chips[0].id);
  });

  test('a free-form turn (no tool steps) produces no tools group', () => {
    let state = reduceChat(initialChatState(), { type: 'send', message: 'ping' });
    state = reduceChat(state, {
      type: 'turn-succeeded',
      response: { session_id: 'sid', turn: [], answer: 'olá!', urls: [] }
    });
    expect(outline(state.items)).toEqual(['user', 'assistant']);
  });

  test('a failed turn adds no tools group and no reply bubble', () => {
    let state = reduceChat(initialChatState(), { type: 'send', message: 'ping' });
    state = reduceChat(state, { type: 'turn-failed', error: 'ECONNREFUSED' });
    expect(outline(state.items)).toEqual(['user']);
    expect(state.pending).toBe(false);
    expect(state.error).toBe('ECONNREFUSED');
  });

  test('transcriptFromRecords interleaves per-turn groups (answers skipped as chips)', () => {
    const records: AgentRecord[] = [
      { role: 'user', content: 'ping', ts: '2026-10-27T12:00:00+00:00' },
      { role: 'assistant', tool: 'app.data', args: {}, result: { ok: true }, ts: 'b' },
      { role: 'assistant', tool: 'notify.test', args: {}, error: 'osascript falhou', ts: 'c' },
      { role: 'assistant', answer: 'feito!', urls: [ARTIFACT_URL], ts: 'd' },
      { role: 'user', content: 'otimizar', ts: '2026-10-27T12:05:00+00:00' },
      { role: 'assistant', tool: 'dashboard.create', args: {}, result: { artifact_id: HEX32 }, ts: 'f' },
      { role: 'assistant', answer: 'pronto', urls: [], ts: 'g' }
    ];
    const { items, artifactUrl } = transcriptFromRecords(records);
    // turn 1: user, tools[2], answer — turn 2: user, tools[1], answer
    expect(outline(items)).toEqual(['user', 2, 'assistant', 'user', 1, 'assistant']);
    expect(artifactUrl).toBe(ARTIFACT_URL);
    const group1 = items[1];
    expect(group1.kind === 'tools' && group1.chips.map((c) => [c.tool, c.ok])).toEqual([
      ['app.data', true],
      ['notify.test', false]
    ]);
    // group ids are unique so per-turn expansion state never collides
    const ids = items.map((item) => item.id);
    expect(new Set(ids).size).toBe(ids.length);
  });

  test('transcriptFromRecords timestamps bubbles from the record ts', () => {
    const records: AgentRecord[] = [
      { role: 'user', content: 'oi', ts: '2026-10-27T12:34:00+00:00' },
      { role: 'assistant', answer: 'olá', urls: [], ts: '2026-10-27T09:05:00-05:00' }
    ];
    const { items } = transcriptFromRecords(records);
    expect(items[0].kind === 'bubble' && items[0].ts).toBe('2026-10-27T12:34:00+00:00');
    expect(items[1].kind === 'bubble' && items[1].ts).toBe('2026-10-27T09:05:00-05:00');
  });
});

// ---- timestamps -----------------------------------------------------------

describe('formatBubbleTime maps ts to local HH:MM', () => {
  test('renders the local wall clock of the timestamp', () => {
    const iso = '2026-01-15T10:05:00+00:00';
    const date = new Date(iso);
    const expected = `${String(date.getHours()).padStart(2, '0')}:${String(date.getMinutes()).padStart(2, '0')}`;
    expect(expected).toMatch(/^\d{2}:\d{2}$/);
    expect(formatBubbleTime(iso)).toBe(expected);
  });

  test('zero-pads single-digit hours and minutes', () => {
    const iso = '2026-01-15T03:07:30+00:00';
    const date = new Date(iso);
    const expected = `${String(date.getHours()).padStart(2, '0')}:${String(date.getMinutes()).padStart(2, '0')}`;
    expect(formatBubbleTime(iso)).toBe(expected);
    expect(expected.startsWith('0') || expected.includes(':0')).toBe(true);
  });

  test('empty and unparsable values render nothing', () => {
    expect(formatBubbleTime(null)).toBe('');
    expect(formatBubbleTime(undefined)).toBe('');
    expect(formatBubbleTime('')).toBe('');
    expect(formatBubbleTime('not-a-date')).toBe('');
  });
});

// ---- prefill / composer draft ---------------------------------------------

describe('prefill and composer draft in the VM', () => {
  test('prefill fills the draft without sending (no bubble, not pending)', () => {
    let state = reduceChat(initialChatState(), {
      type: 'prefill',
      text: 'Draft a spending dashboard for this month'
    });
    expect(state.draft).toBe('Draft a spending dashboard for this month');
    expect(state.items).toHaveLength(0);
    expect(state.pending).toBe(false);
    // draft-changed keeps typing in sync
    state = reduceChat(state, { type: 'draft-changed', text: 'hello there' });
    expect(state.draft).toBe('hello there');
  });

  test('send clears the VM draft and stamps the user bubble with an ISO ts', () => {
    let state = reduceChat(initialChatState(), { type: 'prefill', text: 'draft text' });
    state = reduceChat(state, { type: 'send', message: 'draft text' });
    expect(state.draft).toBe('');
    expect(state.pending).toBe(true);
    const first = state.items[0];
    expect(first.kind === 'bubble' && first.text).toBe('draft text');
    expect(first.kind === 'bubble' && typeof first.ts === 'string' && !Number.isNaN(new Date(first.ts).getTime())).toBe(true);
  });

  test('session-selected reset clears the draft as well', () => {
    const state = reduceChat(initialChatState(), { type: 'prefill', text: 'half typed' });
    const next = reduceChat(state, { type: 'session-selected', sessionId: null });
    expect(next.draft).toBe('');
  });
});

// ---- reduce flow ----------------------------------------------------------

describe('send → succeed flow', () => {
  test('user bubble appears immediately and pending clears on answer', () => {
    let state = initialChatState();
    state = reduceChat(state, { type: 'send', message: 'dashboard, por favor' });
    const first = state.items[0];
    expect(first.kind === 'bubble' && first.role).toBe('user');
    expect(first.kind === 'bubble' && first.text).toBe('dashboard, por favor');
    expect(state.pending).toBe(true);
    expect(state.error).toBeNull();

    state = reduceChat(state, { type: 'turn-succeeded', response: chainResponse() });
    expect(state.pending).toBe(false);
    const answer = state.items[2];
    expect(answer.kind === 'bubble' && [answer.role, answer.text]).toEqual(['assistant', 'Aqui está o dashboard!']);
    expect(state.sessionId).toBe('11111111-2222-3333-4444-555555555555');
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

  test('error-cleared reduces error to null even if previously in an error state', () => {
    const state0 = withState({
      error: 'Previous failure message',
      pending: false,
      items: [{ kind: 'bubble', id: 'm1', role: 'user', text: 'hello', ts: null }]
    });
    const state1 = reduceChat(state0, { type: 'error-cleared' });
    expect(state1.error).toBeNull();
    expect(state1.items).toEqual(state0.items);
    expect(state1.pending).toBe(false);
  });
});

// ---- sessions -------------------------------------------------------------

describe('session selection and reset', () => {
  test('session-selected(null) resets state but keeps the sessions list', () => {
    const state0 = withState({
      sessionId: 'sid-a',
      items: [
        { kind: 'bubble', id: 'b1', role: 'user', text: 'x', ts: null },
        { kind: 'tools', id: 'g1', chips: [{ id: 'g1-0', tool: 'app.data', ok: true, preview: '{}' }] }
      ],
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
    expect(state1.items).toEqual([]);
    expect(state1.sessions).toHaveLength(1); // the picker list survives the reset
  });

  test('sessions-loaded replaces the list', () => {
    const sessions = [{ session_id: 's', updated_at: 'u', turns: 0, preview: 'p' }];
    const state = reduceChat(initialChatState(), { type: 'sessions-loaded', sessions });
    expect(state.sessions).toEqual(sessions);
  });

  test('session-loaded rebuilds the interleaved transcript only for the current session', () => {
    const records: AgentRecord[] = [
      { role: 'user', content: 'dashboard?', ts: 'r1' },
      { role: 'assistant', tool: 'app.data', args: {}, result: {}, ts: 'r2' },
      { role: 'assistant', answer: 'feito!', urls: [ARTIFACT_URL], ts: 'r3' }
    ];
    const state0 = withState({ sessionId: 'sid' });
    const state1 = reduceChat(state0, { type: 'session-loaded', sessionId: 'sid', records });
    expect(state1.items).toHaveLength(3);
    expect(state1.items[0].kind === 'bubble' && state1.items[0].text).toBe('dashboard?');
    expect(state1.items[1].kind).toBe('tools');
    expect(state1.items[2].kind === 'bubble' && state1.items[2].text).toBe('feito!');
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
    expect(state.items.filter((item) => item.kind === 'bubble').map((item) => item.text)).toEqual([
      'dashboard?',
      'pronto!'
    ]);
    expect(state.items[1].kind).toBe('tools');
    expect(state.artifactUrl).toBe(ARTIFACT_URL);
  });
});