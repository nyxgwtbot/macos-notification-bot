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
