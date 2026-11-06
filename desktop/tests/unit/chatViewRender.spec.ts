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
