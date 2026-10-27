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
}

export interface ChatToolChip {
  id: string;
  tool: string;
  ok: boolean;
  /** Pretty-printed result (ok) or error text, truncated to the cap. */
  preview: string;
}

export interface ChatState {
  messages: ChatBubble[];
  chips: ChatToolChip[];
  artifactUrl: string | null;
  pending: boolean;
  error: string | null;
  sessionId: string | null;
  sessions: AgentSessionSummary[];
}

export type ChatEvent =
  | { type: 'send'; message: string }
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
    messages: [],
    chips: [],
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

export function toBubble(role: 'user' | 'assistant', text: string): ChatBubble {
  return { id: nextId('bubble'), role, text };
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
  return { id: `chip-${step.ts}-${index}`, tool: step.tool, ok, preview };
}

/** Rebuild bubbles/chips/artifact from a persisted session transcript. */
export function chipsFromRecords(records: AgentRecord[]): ChatToolChip[] {
  const chips: ChatToolChip[] = [];
  records.forEach((record) => {
    if (record.role === 'assistant' && typeof record.tool === 'string') {
      chips.push(
        toToolChip(
          {
            role: 'assistant',
            tool: record.tool,
            args: record.args,
            result: record.result,
            error: record.error,
            ts: record.ts,
          },
          chips.length
        )
      );
    }
  });
  return chips;
}

export function reduceChat(state: ChatState, event: ChatEvent): ChatState {
  switch (event.type) {
    case 'send':
      return {
        ...state,
        messages: [...state.messages, toBubble('user', event.message)],
        pending: true,
        error: null,
      };
    case 'turn-succeeded': {
      const response = event.response;
      const newUrl = iframeUrlFrom(response?.urls);
      return {
        ...state,
        sessionId: response.session_id,
        messages: [...state.messages, toBubble('assistant', response.answer)],
        chips: [...state.chips, ...response.turn.map((step, index) => toToolChip(step, index))],
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
      const messages: ChatBubble[] = [];
      let artifactUrl = state.artifactUrl;
      event.records.forEach((record) => {
        if (record.role === 'user') {
          messages.push(toBubble('user', String(record.content ?? '')));
        } else if (record.role === 'assistant' && typeof record.answer === 'string') {
          messages.push(toBubble('assistant', record.answer));
          const url = iframeUrlFrom(record.urls ?? []);
          if (url) artifactUrl = url;
        } else if (record.role === 'assistant' && typeof record.content === 'string') {
          // legacy/loop-internal records without a tool or answer payload
          messages.push(toBubble('assistant', record.content));
        }
      });
      return {
        ...state,
        messages,
        chips: chipsFromRecords(event.records),
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