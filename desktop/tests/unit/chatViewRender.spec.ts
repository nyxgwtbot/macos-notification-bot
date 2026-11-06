/**
 * L5-R3 field comfort pass — composer inset proof for ChatView. The composer
 * text inset and the keyboard-hint inset must stay in lockstep (hint's first
 * character aligns with the textarea's first text character). Scoped SFC CSS
 * is not applied in vitest (no css: true in vite.config), so these asserts
 * pin the rules that compile into the view; live shadow-DOM rendering is
 * confirmed in the supervisor's CDP pass.
 */

import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, expect, test } from 'vitest';

const CHAT_VIEW_SOURCE = readFileSync(
  path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../src/views/ChatView.vue'),
  'utf8'
);

describe('ChatView composer field insets (L5-R3)', () => {
  test('composer insets text via Ionic custom properties (12px inline, 0 vertical)', () => {
    const rule = CHAT_VIEW_SOURCE.match(/\.nb-composer-input\s*\{[^}]*\}/);
    expect(rule).not.toBeNull();
    expect(rule?.[0]).toContain('--padding-start: var(--nb-space-3);');
    expect(rule?.[0]).toContain('--padding-end: var(--nb-space-3);');
    expect(rule?.[0]).toContain('--padding-top: 0;');
    expect(rule?.[0]).toContain('--padding-bottom: 0;');
  });

  test('composer hint inset matches the text inset (12px) for first-character alignment', () => {
    const rule = CHAT_VIEW_SOURCE.match(/\.nb-composer-hint\s*\{[^}]*\}/);
    expect(rule).not.toBeNull();
    expect(rule?.[0]).toContain('padding-inline-start: var(--nb-space-3);');
  });
});

describe('ChatView narrow rail policy (L5-R4)', () => {
  test('has-artifact class still toggles from the railOpen binding', () => {
    expect(CHAT_VIEW_SOURCE).toContain(
      `:class="{ 'has-artifact': state.artifactUrl && railOpen }"`
    );
  });

  test('below 800px the artifact rail switches to an overlay drawer (CSS-only)', () => {
    const narrowStart = CHAT_VIEW_SOURCE.indexOf('NARROW_RAIL_MAX = 800');
    expect(narrowStart).toBeGreaterThan(-1);
    const narrowBlock = CHAT_VIEW_SOURCE.slice(narrowStart, CHAT_VIEW_SOURCE.indexOf('</style>'));
    expect(narrowBlock).toContain('NARROW_RAIL_MAX = 800');
    expect(narrowBlock).toContain('.nb-chat-layout.has-artifact');
    expect(narrowBlock).toContain('grid-template-columns: minmax(0, 1fr);');
    expect(narrowBlock).toContain('.nb-chat-rail');
    expect(narrowBlock).toContain('position: absolute;');
    expect(narrowBlock).toContain('width: min(440px, 86%);');
    expect(narrowBlock).toContain('z-index: 10;');
  });
});

describe('ChatView stop-waiting affordance', () => {
  test('progress status carries a quiet stop button wired to stopWaiting', () => {
    const status = CHAT_VIEW_SOURCE.match(/<div class="nb-progress-status">[\s\S]*?<\/div>/);
    expect(status).not.toBeNull();
    expect(status?.[0]).toContain('class="nb-progress-stop"');
    expect(status?.[0]).toContain(
      `:title="'Stop waiting — the turn may still finish and appear here'"`
    );
    expect(status?.[0]).toContain('@click="stopWaiting"');
  });

  test('stopWaiting fails the turn with the exact stop sentence', () => {
    expect(CHAT_VIEW_SOURCE).toContain(
      `error: 'Stopped waiting — the turn may still finish. Reopen this session to check.'`
    );
  });
});

describe('ChatView live progress announcements', () => {
  test('phase span is the only polite live region inside the progress bubble', () => {
    const bubble = CHAT_VIEW_SOURCE.match(/<div v-else-if="item\.kind === 'progress'"[^>]*>/);
    expect(bubble).not.toBeNull();
    expect(bubble?.[0]).toContain('aria-live="off"');
    expect(CHAT_VIEW_SOURCE).toContain(
      '<span class="nb-progress-phase" aria-live="polite">{{ item.phaseLabel }}</span>'
    );
  });
});

describe('ChatView error strip and send affordances', () => {
  test('turn failure strip is an alert and no longer prefixes its copy', () => {
    expect(CHAT_VIEW_SOURCE).toContain('<div v-if="state.error" class="nb-error-strip" role="alert">');
    expect(CHAT_VIEW_SOURCE).toContain('<span>{{ state.error }}</span>');
    expect(CHAT_VIEW_SOURCE).not.toContain('<span>Turn failed: {{ state.error }}</span>');
  });

  test('retry explains its composer refill; send explains why it is disabled', () => {
    expect(CHAT_VIEW_SOURCE).toContain(
      'title="Puts your last message back in the composer"'
    );
    expect(CHAT_VIEW_SOURCE).toContain(
      `:title="state.draft.trim() === '' ? 'Type a message to enable send' : undefined"`
    );
  });

  test('send catch uses the shared turnFailedErrorText helper', () => {
    expect(CHAT_VIEW_SOURCE).toContain(
      `dispatch({ type: 'turn-failed', error: turnFailedErrorText(err) });`
    );
  });
});

describe('ChatView session pill truncation', () => {
  test('select text part ellipsizes long session labels', () => {
    const rule = CHAT_VIEW_SOURCE.match(/\.nb-session-select::part\(text\)\s*\{[^}]*\}/);
    expect(rule).not.toBeNull();
    expect(rule?.[0]).toContain('overflow: hidden;');
    expect(rule?.[0]).toContain('text-overflow: ellipsis;');
    expect(rule?.[0]).toContain('white-space: nowrap;');
  });
});

describe('ChatView tool chip disclosure wiring', () => {
  test('both live and transcript chips link head buttons to their preview ids', () => {
    const controls = CHAT_VIEW_SOURCE.split(':aria-controls="`nb-chip-${chip.id}`"').length - 1;
    const previews = CHAT_VIEW_SOURCE.split(':id="`nb-chip-${chip.id}`"').length - 1;
    expect(controls).toBe(2);
    expect(previews).toBe(2);
  });
});

describe('ChatView daemon dot hit area', () => {
  test('keeps the 9px visual dot but grows the click target via negative margin', () => {
    const rule = CHAT_VIEW_SOURCE.match(/\.nb-daemon-dot\s*\{[^}]*\}/);
    expect(rule).not.toBeNull();
    expect(rule?.[0]).toContain('width: 9px;');
    expect(rule?.[0]).toContain('height: 9px;');
    expect(rule?.[0]).toContain('padding: var(--nb-space-2);');
    expect(rule?.[0]).toContain('margin: calc(-1 * var(--nb-space-2));');
  });
});

describe('ChatView composer draft persistence', () => {
  test('onMounted restores the stored draft before loading sessions', () => {
    const mounted = CHAT_VIEW_SOURCE.indexOf('onMounted(() => {');
    const restore = CHAT_VIEW_SOURCE.indexOf(
      `dispatch({ type: 'draft-changed', text: storedDraft });`
    );
    const load = CHAT_VIEW_SOURCE.indexOf('void loadSessions(true);');
    expect(mounted).toBeGreaterThan(-1);
    expect(restore).toBeGreaterThan(mounted);
    expect(load).toBeGreaterThan(restore);
  });

  test('typing debounces a draft write at 300ms', () => {
    expect(CHAT_VIEW_SOURCE).toContain('const DRAFT_SAVE_DEBOUNCE_MS = 300;');
    const input = CHAT_VIEW_SOURCE.slice(
      CHAT_VIEW_SOURCE.indexOf('function onDraftInput'),
      CHAT_VIEW_SOURCE.indexOf('function onComposerKeydown')
    );
    expect(input).toContain('window.clearTimeout(draftSaveTimer);');
    expect(input).toContain('window.setTimeout(() => {');
    expect(input).toContain('writeStoredDraft(state.value.draft);');
  });

  test('sending clears the persisted draft and unmount clears the timer', () => {
    const sendBlock = CHAT_VIEW_SOURCE.slice(
      CHAT_VIEW_SOURCE.indexOf('async function send()'),
      CHAT_VIEW_SOURCE.indexOf('function retryLast')
    );
    expect(sendBlock).toContain('clearStoredDraft();');
    const unmounted = CHAT_VIEW_SOURCE.slice(
      CHAT_VIEW_SOURCE.indexOf('onUnmounted(() => {'),
      CHAT_VIEW_SOURCE.indexOf('</script>')
    );
    expect(unmounted).toContain('window.clearTimeout(draftSaveTimer);');
  });
});
