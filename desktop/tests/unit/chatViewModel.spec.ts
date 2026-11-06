import { describe, expect, test, afterEach, vi } from 'vitest';

import {
  extractToolTarget,
  formatBubbleTime,
  formatTurnElapsed,
  formatTurnPhase,
  iframeUrlFrom,
  initialChatState,
  isIpcTimeout,
  reduceChat,
  smoothProgressPreview,
  toToolChip,
  toolSummaryFromChips,
  transcriptFromRecords,
  type ChatState,
  type ChatTranscriptItem
} from '../../src/viewModels/chatViewModel';
import { NotifyClient } from '../../src/services/notifyClient';
import type { NotifyAPI } from '../../src/notify';
import type {
  AgentActiveTurnResponse,
  AgentRecord,
  AgentSessionDetail,
  AgentTurnResponse
} from '../../src/types/bot';

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

/** Compact item outline: bubble -> 'user'/'assistant'; tools -> chips count;
 * transient progress -> 'progress' (never present after completion). */
function outline(items: ChatTranscriptItem[]): Array<string | number> {
  return items.map((item) => {
    if (item.kind === 'bubble') return item.role;
    if (item.kind === 'progress') return 'progress';
    return item.chips.length;
  });
}

/** Local live-turn fixture; never imported from ChatView. */
function activeTurn(partial: Partial<AgentActiveTurnResponse> = {}): AgentActiveTurnResponse {
  return {
    session_id: 'sid-live',
    phase: 'thinking',
    attempt: 1,
    started_at: '2026-01-01T00:00:00.000Z',
    current_tool: null,
    tool_started_at: null,
    completed_tools: [],
    raw_partial: '',
    done: false,
    ...partial
  };
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

  test('extracts and truncates target to 24 chars, skipping html key', () => {
    const chipWithDataset = toToolChip(
      { role: 'assistant', tool: 'app.data', args: { dataset: 'health' }, result: { ok: true }, ts: 't1' },
      0
    );
    expect(chipWithDataset.target).toBe('health');

    const longTitle = 'Financial spending and summary for October 2026';
    const chipWithLongTitle = toToolChip(
      { role: 'assistant', tool: 'dashboard.create', args: { html: '<html>...</html>', title: longTitle }, result: { ok: true }, ts: 't2' },
      1
    );
    expect(chipWithLongTitle.target).toBe(longTitle.slice(0, 24));
    expect(chipWithLongTitle.target.length).toBe(24);

    const chipNoString = toToolChip(
      { role: 'assistant', tool: 'notify.test', args: {}, result: { ok: true }, ts: 't3' },
      2
    );
    expect(chipNoString.target).toBe('');
  });

  test('toolSummaryFromChips formats unique tools and repeat counts in first-seen order', () => {
    expect(toolSummaryFromChips([])).toBe('');

    const chips = [
      { id: '1', tool: 'app.data', ok: true, preview: '', target: '' },
      { id: '2', tool: 'app.data', ok: true, preview: '', target: '' },
      { id: '3', tool: 'dashboard.create', ok: true, preview: '', target: '' },
      { id: '4', tool: 'server.serve', ok: true, preview: '', target: '' }
    ];
    expect(toolSummaryFromChips(chips)).toBe('app.data ×2 · dashboard.create · server.serve');

    const singleChip = [{ id: '1', tool: 'notify.test', ok: true, preview: '', target: '' }];
    expect(toolSummaryFromChips(singleChip)).toBe('notify.test');
  });
});

// ---- live turn formatting helpers ----------------------------------------

describe('extractToolTarget', () => {
  test('skips html, slices to 24 chars and falls back to empty string', () => {
    expect(extractToolTarget(null)).toBe('');
    expect(extractToolTarget(undefined)).toBe('');
    expect(extractToolTarget({})).toBe('');
    expect(extractToolTarget({ html: '<html>…</html>' })).toBe('');
    expect(extractToolTarget({ html: '<html>', dataset: 'health' })).toBe('health');

    const longTitle = 'Financial spending and summary for October 2026';
    const target = extractToolTarget({ html: '<html>', title: longTitle });
    expect(target).toBe(longTitle.slice(0, 24));
    expect(target).toHaveLength(24);
  });
});

describe('formatTurnElapsed', () => {
  test('null/invalid timestamps render 0s', () => {
    expect(formatTurnElapsed(null)).toBe('Working · 0s');
    expect(formatTurnElapsed(undefined)).toBe('Working · 0s');
    expect(formatTurnElapsed('not-a-date')).toBe('Working · 0s');
  });

  test('renders whole elapsed seconds against an explicit clock', () => {
    const startedAt = '2026-01-01T00:00:00.000Z';
    const parsed = Date.parse(startedAt);
    expect(formatTurnElapsed(startedAt, parsed + 12_000)).toBe('Working · 12s');
    expect(formatTurnElapsed(startedAt, parsed + 1_500)).toBe('Working · 1s');
  });

  test('clamps a clock ahead of startedAt to 0s', () => {
    const startedAt = '2026-01-01T00:00:00.000Z';
    expect(formatTurnElapsed(startedAt, Date.parse(startedAt) - 5_000)).toBe('Working · 0s');
  });
});

describe('formatTurnPhase', () => {
  test('maps every loop phase to human copy', () => {
    expect(formatTurnPhase({ phase: 'thinking', attempt: 1, current_tool: null })).toBe('Thinking…');
    expect(formatTurnPhase({ phase: 'retry', attempt: 2, current_tool: null })).toBe(
      'Reformatting response (retry 2)'
    );
    expect(formatTurnPhase({ phase: 'tool', attempt: 1, current_tool: { tool: 'app.data', target: 'health' } })).toBe(
      'Running tool app.data…'
    );
    expect(formatTurnPhase({ phase: 'tool', attempt: 1, current_tool: null })).toBe('Running tool…');
    expect(formatTurnPhase({ phase: 'serving', attempt: 1, current_tool: null })).toBe('Serving dashboard…');
    // unknown phases fall back to thinking copy
    expect(formatTurnPhase({ phase: 'idle', attempt: 0, current_tool: null })).toBe('Thinking…');
  });

  test('clamps the retry attempt to at least 1', () => {
    expect(formatTurnPhase({ phase: 'retry', attempt: 0, current_tool: null })).toBe(
      'Reformatting response (retry 1)'
    );
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
    if (groupA.kind !== 'tools' || groupB.kind !== 'tools') throw new Error('expected tools groups');
    expect(groupA.chips[0].id).not.toBe(groupB.chips[0].id);
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

  test('transcriptFromRecords keeps one group per turn across loop-internal assistant_raw rows', () => {
    // Real persisted sequence (agent.py): every parsed record is preceded by a
    // loop-internal assistant_raw row; the auto server.serve step is not.
    const rawRow = { role: 'assistant_raw', phase: 'attempt', raw_model_output: '{"tool":"app.data"}', ts: 'raw' } as AgentRecord;
    const records: AgentRecord[] = [
      { role: 'user', content: 'dashboard, por favor', ts: 'r1' },
      { ...rawRow, ts: 'raw-a' },
      { role: 'assistant', tool: 'app.data', args: {}, result: { ok: true }, ts: 'a1' },
      { ...rawRow, ts: 'raw-b' },
      { role: 'assistant', tool: 'app.data', args: {}, result: { ok: true }, ts: 'a2' },
      { ...rawRow, ts: 'raw-c' },
      { role: 'assistant', tool: 'dashboard.create', args: { title: 'Gastos' }, result: { artifact_id: HEX32 }, ts: 'a3' },
      { role: 'assistant', tool: 'server.serve', args: { artifact_id: HEX32 }, result: { url: ARTIFACT_URL }, ts: 'a4' },
      { ...rawRow, ts: 'raw-d' },
      { role: 'assistant', answer: 'Aqui está o dashboard!', urls: [ARTIFACT_URL], ts: 'a5' }
    ];
    const { items, artifactUrl } = transcriptFromRecords(records);
    // [user bubble, ONE tools group (4 chips), assistant bubble] — raw rows never flush
    expect(outline(items)).toEqual(['user', 4, 'assistant']);
    const group = items[1];
    expect(group.kind === 'tools' && group.chips.map((c) => [c.tool, c.ok])).toEqual([
      ['app.data', true],
      ['app.data', true],
      ['dashboard.create', true],
      ['server.serve', true]
    ]);
    expect(items[2].kind === 'bubble' && [items[2].role, items[2].text]).toEqual(['assistant', 'Aqui está o dashboard!']);
    expect(artifactUrl).toBe(ARTIFACT_URL);
    // raw rows must not contribute items or chip ids of their own
    const ids = items.map((item) => item.id);
    expect(new Set(ids).size).toBe(ids.length);
  });

  test('transcriptFromRecords attaches tail-capped thinking to the tools group', () => {
    const longRaw = 'x'.repeat(500);
    const records: AgentRecord[] = [
      { role: 'user', content: 'q', ts: 'r1' },
      { role: 'assistant_raw', phase: 'attempt', raw_model_output: longRaw, ts: 'raw' },
      { role: 'assistant', tool: 'app.data', args: {}, result: {}, ts: 't1' },
      { role: 'assistant', answer: 'ok', urls: [], ts: 'a1' }
    ];
    const { items } = transcriptFromRecords(records);
    expect(outline(items)).toEqual(['user', 1, 'assistant']);
    const group = items[1];
    expect(group.kind === 'tools' && group.thinking).toBe(longRaw.slice(-400));
  });

  test('transcriptFromRecords attaches thinking to the answer bubble when no tools ran', () => {
    const records: AgentRecord[] = [
      { role: 'user', content: 'q', ts: 'r1' },
      { role: 'assistant_raw', raw_model_output: 'reasoning ', ts: 'raw-a' },
      { role: 'assistant_raw', raw_model_output: 'trace', ts: 'raw-b' },
      { role: 'assistant', answer: 'ok', urls: [], ts: 'a1' }
    ];
    const { items } = transcriptFromRecords(records);
    expect(outline(items)).toEqual(['user', 'assistant']);
    const answer = items[1];
    expect(answer.kind === 'bubble' && answer.thinking).toBe('reasoning \ntrace');
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

// ---- smoothProgressPreview ----
describe('smoothProgressPreview', () => {
  test('extracts the answer text from a complete persisted JSON tail (live failing case)', () => {
    expect(
      smoothProgressPreview('{"answer": "Painel de gastos de novembro/2026 pronto!", "urls": ["http://x"]}'),
    ).toBe('Painel de gastos de novembro/2026 pronto!');
  });

  test('still returns the answer when the closed brace is followed by trailing garbage', () => {
    expect(
      smoothProgressPreview('{"answer": "Painel de gastos de novembro/2026 pronto!", "urls": ["http://x"]} {"partia'),
    ).toBe('Painel de gastos de novembro/2026 pronto!');
  });

  test('keeps the last 140 characters with an ellipsis for an unterminated mid-stream answer', () => {
    const body = 'x'.repeat(160);
    expect(smoothProgressPreview(`{"answer": "${body}`)).toBe(`…${body.slice(-140)}`);
  });

  test('returns the in-progress answer text from an unterminated JSON tail', () => {
    expect(smoothProgressPreview('{"answer": "Painel de gastos de novembro/2026 pronto!')).toBe(
      'Painel de gastos de novembro/2026 pronto!',
    );
  });

  test('unescapes newline escapes inside the answer for display', () => {
    expect(smoothProgressPreview('{"answer": "line one\\nline two"}')).toBe('line one\nline two');
    expect(smoothProgressPreview('{"answer": "line one\\nline two')).toBe('line one\nline two');
  });

  test('announces the tool name being streamed', () => {
    expect(smoothProgressPreview('{"tool": "app.data')).toBe('Preparing tool app.data…');
  });

  test('reports a completed tool call from full tool JSON', () => {
    expect(smoothProgressPreview('{"tool": "query_data"}')).toBe('Prepared tool query_data');
  });

  test('passes plain thinking text through unchanged', () => {
    expect(smoothProgressPreview('weighing the options')).toBe('weighing the options');
  });

  test('returns an empty string for empty or whitespace input', () => {
    expect(smoothProgressPreview('')).toBe('');
    expect(smoothProgressPreview('   \n  ')).toBe('');
  });

  test('preserves escaped quotes inside the in-progress answer', () => {
    expect(smoothProgressPreview('{"answer": "He said \\"hi')).toBe('He said \\"hi');
  });
});

// ---- live turn-progress reducer ------------------------------------------

describe('turn-progress live item', () => {
  test('inserts the progress item after the user bubble while pending', () => {
    let state = reduceChat(initialChatState(), { type: 'send', message: 'dashboard, por favor' });
    state = reduceChat(state, {
      type: 'turn-progress',
      progress: activeTurn({
        phase: 'tool',
        attempt: 1,
        current_tool: { tool: 'dashboard.create', target: 'Gastos', args: { html: '<html>', title: 'Gastos' } },
        completed_tools: [
          { role: 'assistant', tool: 'app.data', args: { dataset: 'health' }, result: { ok: true }, ts: 't1' },
          {
            role: 'assistant',
            tool: 'dashboard.create',
            args: { html: '<html>', title: 'Gastos' },
            result: { artifact_id: HEX32 },
            ts: 't2'
          }
        ],
        raw_partial: '{"tool":'
      })
    });

    expect(outline(state.items)).toEqual(['user', 'progress']);
    const item = state.items[1];
    expect(item.kind).toBe('progress');
    if (item.kind !== 'progress') throw new Error('expected progress item');
    expect(item.phase).toBe('tool');
    expect(item.phaseLabel).toBe('Running tool dashboard.create…');
    expect(item.attempt).toBe(1);
    expect(item.currentTool).toEqual({ tool: 'dashboard.create', target: 'Gastos' });
    expect(item.completedChips.map((chip) => [chip.tool, chip.target])).toEqual([
      ['app.data', 'health'],
      ['dashboard.create', 'Gastos']
    ]);
    expect(item.rawPartial).toBe('{"tool":');
    expect(state.pending).toBe(true);
  });

  test('backend-redacted html in completed args stays opaque and never becomes the target', () => {
    let state = reduceChat(initialChatState(), { type: 'send', message: 'dashboard' });
    state = reduceChat(state, {
      type: 'turn-progress',
      progress: activeTurn({
        phase: 'tool',
        completed_tools: [
          {
            role: 'assistant',
            tool: 'dashboard.create',
            args: { title: 'Ops', html: '[redacted html: 4123 chars]' },
            result: { ok: true },
            ts: 't1'
          }
        ]
      })
    });

    const item = state.items[1];
    expect(item.kind).toBe('progress');
    if (item.kind !== 'progress') throw new Error('expected progress item');
    expect(item.completedChips).toHaveLength(1);
    const [chip] = item.completedChips;
    expect(chip.target).toBe('Ops');
    expect(chip.ok).toBe(true);
    expect(chip.target).not.toContain('redacted');
    expect(chip.preview).not.toContain('redacted html');
  });

  test('updates in place, preserving the item id and last non-empty rawPartial', () => {
    let state = reduceChat(initialChatState(), { type: 'send', message: 'go' });
    state = reduceChat(state, {
      type: 'turn-progress',
      progress: activeTurn({ phase: 'thinking', raw_partial: 'partial text' })
    });
    const first = state.items[1];
    expect(first.kind).toBe('progress');
    const id = first.id;

    state = reduceChat(state, {
      type: 'turn-progress',
      progress: activeTurn({
        phase: 'tool',
        current_tool: { tool: 'app.data', target: 'health' },
        raw_partial: ''
      })
    });

    expect(state.items).toHaveLength(2);
    const second = state.items[1];
    expect(second.kind).toBe('progress');
    if (second.kind !== 'progress') throw new Error('expected progress item');
    expect(second.id).toBe(id);
    expect(second.rawPartial).toBe('partial text');
    expect(second.phaseLabel).toBe('Running tool app.data…');
  });

  test('ignores late progress when the turn is no longer pending', () => {
    const state = withState({ pending: false });
    expect(reduceChat(state, { type: 'turn-progress', progress: activeTurn() })).toBe(state);
  });

  test('ignores a done snapshot that has no live item to update', () => {
    const state = reduceChat(initialChatState(), { type: 'send', message: 'go' });
    expect(
      reduceChat(state, { type: 'turn-progress', progress: activeTurn({ phase: 'done', done: true }) })
    ).toBe(state);
  });

  test('ignores a progress snapshot from a different session', () => {
    const state = withState({ pending: true, sessionId: 'sid-a' });
    expect(
      reduceChat(state, { type: 'turn-progress', progress: activeTurn({ session_id: 'sid-b' }) })
    ).toBe(state);
  });

  test('adopts the event session id on the item for a brand-new chat', () => {
    let state = reduceChat(initialChatState(), { type: 'send', message: 'first turn' });
    state = reduceChat(state, {
      type: 'turn-progress',
      progress: activeTurn({ session_id: 'new-sid' })
    });
    const item = state.items[1];
    expect(item.kind).toBe('progress');
    expect(item.kind === 'progress' && item.sessionId).toBe('new-sid');
    // POST /api/agent/turn alone commits state.sessionId on success.
    expect(state.sessionId).toBeNull();
  });

  test('turn-succeeded replaces progress with the tools group + bubble, preserving thinking', () => {
    let state = reduceChat(initialChatState(), { type: 'send', message: 'dashboard' });
    state = reduceChat(state, {
      type: 'turn-progress',
      progress: activeTurn({ phase: 'tool', raw_partial: 'thinking so far' })
    });

    const response = chainResponse();
    state = reduceChat(state, { type: 'turn-succeeded', response });

    expect(outline(state.items)).toEqual(['user', response.turn.length, 'assistant']);
    expect(state.items.some((item) => item.kind === 'progress')).toBe(false);
    const group = state.items[1];
    expect(group.kind).toBe('tools');
    expect(group.kind === 'tools' && group.thinking).toBe('thinking so far');
  });

  test('turn-succeeded attaches thinking to the bubble when the turn ran no tools', () => {
    let state = reduceChat(initialChatState(), { type: 'send', message: 'ping' });
    state = reduceChat(state, {
      type: 'turn-progress',
      progress: activeTurn({ phase: 'thinking', raw_partial: 'reasoning' })
    });
    state = reduceChat(state, {
      type: 'turn-succeeded',
      response: { session_id: 'sid', turn: [], answer: 'olá!', urls: [] }
    });

    expect(outline(state.items)).toEqual(['user', 'assistant']);
    const answer = state.items[1];
    expect(answer.kind === 'bubble' && answer.thinking).toBe('reasoning');
  });

  test('turn-failed removes the progress item and clears pending', () => {
    let state = reduceChat(initialChatState(), { type: 'send', message: 'go' });
    state = reduceChat(state, { type: 'turn-progress', progress: activeTurn() });
    state = reduceChat(state, { type: 'turn-failed', error: 'ECONNREFUSED' });

    expect(outline(state.items)).toEqual(['user']);
    expect(state.pending).toBe(false);
    expect(state.error).toBe('ECONNREFUSED');
  });
});