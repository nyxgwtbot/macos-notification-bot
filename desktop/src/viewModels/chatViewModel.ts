/**
 * Chat view-model: pure state machine for the Chat tab
 * (tests: tests/unit/chatViewModel.spec.ts). Mirrors eventViewModel.ts
 * conventions: no Ionic imports, pure reduce over typed events, exported
 * mapping helpers.
 */

import type { AgentRecord, AgentSessionSummary, AgentTurnResponse, AgentTurnStep } from '@/types/bot';

/** Defense-in-depth allowlist: only 4770-loopback artifact URLs (32-hex ids,
 * matching what dashboard.create generates) may reach iframe.src. */
export const ARTIFACT_URL_PATTERN = /^http:\/\/127\.0\.0\.1:4770\/([0-9a-f]{32})\.html$/;

/** Tool chip preview cap (~2 KB pretty-printed JSON). */
export const TOOL_CHIP_PREVIEW_MAX_CHARS = 2000;

export interface ChatBubble {
  id: string;
  role: 'user' | 'assistant';
  text: string;
  /** ISO timestamp of the message (server ts on transcript reload, arrival
   * time for live turns); null when unknowable. Rendered HH:MM. */
  ts: string | null;
}

export interface ChatToolChip {
  id: string;
  tool: string;
  ok: boolean;
  /** Pretty-printed result (ok) or error text, truncated to the cap. */
  preview: string;
  /** First string value of step.args truncated to 24 chars, '' when absent (skips 'html' key). */
  target: string;
}

/** One transcript entry in display order. A turn's tool steps are interleaved
 * as a single collapsible group directly under that turn's user bubble and
 * before its assistant reply (kind: 'tools'). */
export type ChatTranscriptItem =
  | ({ kind: 'bubble' } & ChatBubble)
  | ({ kind: 'tools'; id: string; chips: ChatToolChip[] });

export interface ChatState {
  /** Interleaved transcript: bubbles + per-turn tool groups in display order. */
  items: ChatTranscriptItem[];
  /** Composer draft owned by the VM so prefill/send/locked states are pure. */
  draft: string;
  artifactUrl: string | null;
  pending: boolean;
  error: string | null;
  sessionId: string | null;
  sessions: AgentSessionSummary[];
}

export type ChatEvent =
  | { type: 'send'; message: string }
  /** Suggestion click / retry: fill the draft, focus is a view concern. */
  | { type: 'prefill'; text: string }
  /** Composer keystrokes (keeps send-on-enter semantics view-side). */
  | { type: 'draft-changed'; text: string }
  | { type: 'turn-succeeded'; response: AgentTurnResponse }
  | { type: 'turn-failed'; error: string }
  | { type: 'error-cleared' }
  | { type: 'sessions-loaded'; sessions: AgentSessionSummary[] }
  /** null -> "New session" (full reset); id -> switch (view then loads it). */
  | { type: 'session-selected'; sessionId: string | null }
  /** Server transcript arrived after switching sessions or recovering an
   * IPC-timeout turn. */
  | { type: 'session-loaded'; sessionId: string; records: AgentRecord[] };

export function initialChatState(): ChatState {
  return {
    items: [],
    draft: '',
    artifactUrl: null,
    pending: false,
    error: null,
    sessionId: null,
    sessions: [],
  };
}

/** First artifact URL from answer.urls shaped exactly `<id>.html` on 4770. */
export function iframeUrlFrom(urls: string[] | null | undefined): string | null {
  if (!Array.isArray(urls)) return null;
  for (const url of urls) {
    if (typeof url !== 'string') continue;
    const match = ARTIFACT_URL_PATTERN.exec(url);
    if (match && !match[1].includes('..')) return url;
  }
  return null;
}

/** True when a thrown error looks like the bot-api bridge proxy timeout
 * (botApiIpc.ts rejects with "timeout da chamada ao bot API..."). */
export function isIpcTimeout(err: unknown): boolean {
  const message = err instanceof Error ? err.message : String(err ?? '');
  return /timeout|timed out/i.test(message);
}

/** Deterministic node ids from a monotonic counter carried in the state. */
let seqCounter = 0;
function nextId(prefix: string): string {
  seqCounter += 1;
  return `${prefix}-seq-${seqCounter}`;
}

/** Arrival-time ISO timestamp for bubbles created from live events. */
function nowIso(): string {
  return new Date().toISOString();
}

export function toBubble(role: 'user' | 'assistant', text: string, ts: string | null = null): ChatBubble {
  return { id: nextId('bubble'), role, text, ts };
}

/** ISO timestamp -> local wall clock 'HH:MM' (empty when unparsable). */
export function formatBubbleTime(ts: string | null | undefined): string {
  if (!ts) return '';
  const date = new Date(ts);
  if (Number.isNaN(date.getTime())) return '';
  const two = (n: number) => String(n).padStart(2, '0');
  return `${two(date.getHours())}:${two(date.getMinutes())}`;
}

/** Pretty-printed preview of one tool step: result JSON on success, error
 * text otherwise, truncated to TOOL_CHIP_PREVIEW_MAX_CHARS. */
export function toToolChip(step: AgentTurnStep, index: number): ChatToolChip {
  const ok = typeof step.error !== 'string';
  let preview: string;
  if (!ok) {
    preview = String(step.error ?? 'unknown tool error');
  } else if (step.result === undefined || step.result === null) {
    preview = '(empty result)';
  } else {
    try {
      preview = JSON.stringify(step.result, null, 2);
    } catch {
      preview = String(step.result);
    }
  }
  if (preview.length > TOOL_CHIP_PREVIEW_MAX_CHARS) {
    preview = `${preview.slice(0, TOOL_CHIP_PREVIEW_MAX_CHARS)}\n…[truncated]`;
  }
  let target = '';
  if (step.args && typeof step.args === 'object') {
    for (const [key, val] of Object.entries(step.args as Record<string, unknown>)) {
      if (key === 'html') continue;
      if (typeof val === 'string') {
        target = val.slice(0, 24);
        break;
      }
    }
  }
  return { id: `chip-${step.ts}-${index}`, tool: step.tool, ok, preview, target };
}

/** Deduped tool names in first-seen order with " ×N" for repeats joined by " · ". */
export function toolSummaryFromChips(chips: ChatToolChip[]): string {
  if (!chips || chips.length === 0) return '';
  const counts = new Map<string, number>();
  const order: string[] = [];
  for (const chip of chips) {
    const tool = chip.tool;
    if (!counts.has(tool)) {
      order.push(tool);
      counts.set(tool, 1);
    } else {
      counts.set(tool, (counts.get(tool) ?? 0) + 1);
    }
  }
  return order
    .map((tool) => {
      const count = counts.get(tool) ?? 1;
      return count > 1 ? `${tool} \u00d7${count}` : tool;
    })
    .join(' \u00b7 ');
}

/** One collapsible tool-activity group for a turn's steps, ids scoped to the
 * group so they stay unique across turns. */
function toolsItemFromSteps(steps: AgentTurnStep[]): ChatTranscriptItem {
  const id = nextId('tools');
  return {
    kind: 'tools',
    id,
    chips: steps.map((step, index) => ({ ...toToolChip(step, index), id: `${id}-${index}` })),
  };
}

/** Rebuild the interleaved transcript (bubbles + per-turn tool groups) and
 * artifact URL from a persisted session transcript. Consecutive tool rows
 * form the group of the turn whose user message precedes them; any answer /
 * content row closes the group. Loop-internal rows (assistant_raw / phase
 * attempts) are ignored. */
export function transcriptFromRecords(records: AgentRecord[]): { items: ChatTranscriptItem[]; artifactUrl: string | null } {
  const items: ChatTranscriptItem[] = [];
  let artifactUrl: string | null = null;
  let toolRun: AgentTurnStep[] | null = null;

  const flushTools = (): void => {
    if (toolRun === null) return;
    if (toolRun.length > 0) items.push(toolsItemFromSteps(toolRun));
    toolRun = null;
  };

  records.forEach((record) => {
    if (record.role === 'user') {
      flushTools();
      items.push({ kind: 'bubble', ...toBubble('user', String(record.content ?? ''), record.ts) });
    } else if (record.role === 'assistant' && typeof record.tool === 'string') {
      (toolRun ??= []).push({
        role: 'assistant',
        tool: record.tool,
        args: record.args,
        result: record.result,
        error: record.error,
        ts: record.ts,
      });
    } else if (record.role === 'assistant' && typeof record.answer === 'string') {
      flushTools();
      items.push({ kind: 'bubble', ...toBubble('assistant', record.answer, record.ts) });
      const url = iframeUrlFrom(record.urls ?? []);
      if (url) artifactUrl = url;
    } else if (record.role === 'assistant' && typeof record.content === 'string') {
      // legacy/loop-internal records without a tool or answer payload
      flushTools();
      items.push({ kind: 'bubble', ...toBubble('assistant', record.content, record.ts) });
    } else {
      // Loop-internal rows (assistant_raw / phase attempts) NEVER flush;
      // flushes happen only at real boundaries (user / answer / content) and at transcript end.
    }
  });
  flushTools();
  return { items, artifactUrl };
}

export function reduceChat(state: ChatState, event: ChatEvent): ChatState {
  switch (event.type) {
    case 'send':
      return {
        ...state,
        items: [...state.items, { kind: 'bubble', ...toBubble('user', event.message, nowIso()) }],
        draft: '',
        pending: true,
        error: null,
      };
    case 'prefill':
      return { ...state, draft: event.text };
    case 'draft-changed':
      return { ...state, draft: event.text };
    case 'turn-succeeded': {
      const response = event.response;
      const newUrl = iframeUrlFrom(response?.urls);
      const items: ChatTranscriptItem[] = [...state.items];
      // F1: the turn's tool steps land as one group under this turn's user
      // bubble, before its assistant reply (both are appended in order).
      if (response.turn && response.turn.length > 0) {
        items.push(toolsItemFromSteps(response.turn));
      }
      items.push({ kind: 'bubble', ...toBubble('assistant', response.answer, nowIso()) });
      return {
        ...state,
        sessionId: response.session_id,
        items,
        artifactUrl: newUrl ?? state.artifactUrl,
        pending: false,
        error: null,
        // keep sessions list untouched here; the view refreshes it separately
      };
    }
    case 'turn-failed':
      return { ...state, pending: false, error: event.error };
    case 'error-cleared':
      return { ...state, error: null };
    case 'sessions-loaded':
      return { ...state, sessions: event.sessions };
    case 'session-selected':
      return {
        ...initialChatState(),
        sessions: state.sessions,
        sessionId: event.sessionId,
      };
    case 'session-loaded': {
      // Guard against a stale transcript arriving after the user switched.
      if (state.sessionId !== event.sessionId) return state;
      const { items, artifactUrl } = transcriptFromRecords(event.records);
      return {
        ...state,
        items,
        artifactUrl,
        pending: false,
        error: null,
      };
    }
    default: {
      const exhaustive: never = event;
      return exhaustive;
    }
  }
}