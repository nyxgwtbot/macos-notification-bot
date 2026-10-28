/**
 * R7 config dirty-guard: pristine-vs-dirty snapshot compare, the discard
 * double-arm reducer, and the save/restart SR announce strings. Pure
 * functions from src/viewModels/configDirtyViewModel.ts.
 */

import { describe, expect, test } from 'vitest';

import type { BotRule } from '../../src/types/bot';
import {
  DIRTY_GUARD_CLEAN,
  RESTART_APPLY_ANNOUNCE,
  SAVE_FAILED_ANNOUNCE,
  SETTINGS_SAVED_ANNOUNCE,
  configDraftSnapshot,
  draftIsDirty,
  reduceDirtyGuard,
  type DirtyTrackedDraft
} from '../../src/viewModels/configDirtyViewModel';

const RULES: BotRule[] = [
  { name: 'bank', condition: { app: 'bank' }, actions: ['telegram'] }
];

function draft(overrides: Partial<DirtyTrackedDraft> = {}): DirtyTrackedDraft {
  return {
    pollInterval: '15',
    ollamaUrl: 'http://127.0.0.1:11434',
    ollamaModel: 'llama3',
    ollamaTimeout: '30',
    minConfidence: '0.5',
    actions: [
      { id: 'open_bank', type: 'command', enabled: true, match_hint: 'Fatura', command: 'open bank.app' },
      { id: 'notify', type: 'builtin', enabled: false, match_hint: '', command: '' }
    ],
    rules: RULES,
    mode: 'structured',
    rawRules: JSON.stringify(RULES, null, 2),
    ...overrides
  };
}

describe('SR announce strings (R7)', () => {
  test('exact copy for save success / failure / confirmed_restart', () => {
    expect(SETTINGS_SAVED_ANNOUNCE).toBe('Settings saved.');
    expect(SAVE_FAILED_ANNOUNCE).toBe('Save failed.');
    expect(RESTART_APPLY_ANNOUNCE).toBe('Restart scheduled to apply.');
  });
});

describe('configDraftSnapshot / draftIsDirty (R7)', () => {
  test('a structurally equal draft is pristine; null snapshot is never dirty', () => {
    const snapshot = configDraftSnapshot(draft());
    expect(draftIsDirty(snapshot, draft())).toBe(false);
    expect(draftIsDirty(null, draft())).toBe(false);
  });

  test('every tracked field edit flips dirty', () => {
    const snapshot = configDraftSnapshot(draft());
    for (const field of ['pollInterval', 'ollamaUrl', 'ollamaModel', 'ollamaTimeout', 'minConfidence'] as const) {
      const edited = draft({ [field]: '31' } as Partial<DirtyTrackedDraft>);
      expect(draftIsDirty(snapshot, edited), field).toBe(true);
    }
  });

  test('an action toggle flips dirty; toggling back reverts to pristine', () => {
    const snapshot = configDraftSnapshot(draft());
    const toggled = draft();
    toggled.actions[1].enabled = true;
    expect(draftIsDirty(snapshot, toggled)).toBe(true);
    toggled.actions[1].enabled = false;
    expect(draftIsDirty(snapshot, toggled)).toBe(false);
  });

  test('match_hint / command edits flip dirty', () => {
    const snapshot = configDraftSnapshot(draft());
    const hinted = draft();
    hinted.actions[1].match_hint = 'Fatura paga';
    expect(draftIsDirty(snapshot, hinted)).toBe(true);
    const commanded = draft();
    commanded.actions[0].command = 'open -a Bank.app';
    expect(draftIsDirty(snapshot, commanded)).toBe(true);
  });

  test('a no-op switch to raw mode stays pristine (formatting-insensitive canonical)', () => {
    const snapshot = configDraftSnapshot(draft());
    expect(draftIsDirty(snapshot, draft({ mode: 'raw' }))).toBe(false);
  });

  test('reformatted-but-equal raw JSON stays pristine (whitespace-insensitive canonical)', () => {
    const snapshot = configDraftSnapshot(draft());
    // 4-space indentation, and leading/trailing whitespace — both parse back
    // to the same rules, so neither counts as an edit
    expect(draftIsDirty(snapshot, draft({ mode: 'raw', rawRules: JSON.stringify(RULES, null, 4) }))).toBe(false);
    expect(draftIsDirty(snapshot, draft({ mode: 'raw', rawRules: '  ' + JSON.stringify(RULES) + '\n' }))).toBe(false);
  });

  test('raw JSON with different content is dirty', () => {
    const snapshot = configDraftSnapshot(draft());
    const changed = draft({
      mode: 'raw',
      rawRules: JSON.stringify([{ name: 'other', condition: {}, actions: ['x'] }], null, 2)
    });
    expect(draftIsDirty(snapshot, changed)).toBe(true);
  });

  test('raw JSON that does not parse is dirty', () => {
    const snapshot = configDraftSnapshot(draft());
    expect(draftIsDirty(snapshot, draft({ mode: 'raw', rawRules: '{"name":' }))).toBe(true);
  });

  test('a structured rules edit is dirty, even after mirroring rawRules', () => {
    const snapshot = configDraftSnapshot(draft());
    const edited = draft();
    edited.rules = [{ ...RULES[0], name: 'bank2' }];
    edited.rawRules = JSON.stringify(edited.rules, null, 2);
    expect(draftIsDirty(snapshot, edited)).toBe(true);
  });
});

describe('reduceDirtyGuard (R7 discard double-arm)', () => {
  test('clean stays pristine on arm (button is disabled there anyway)', () => {
    expect(reduceDirtyGuard(DIRTY_GUARD_CLEAN, { type: 'arm' })).toEqual(DIRTY_GUARD_CLEAN);
  });

  test('dirty-change(true) arms nothing; it flips clean -> dirty only', () => {
    expect(reduceDirtyGuard(DIRTY_GUARD_CLEAN, { type: 'dirty-change', dirty: true })).toEqual({
      dirty: true,
      armed: false
    });
  });

  test('further edits while dirty keep plain dirty', () => {
    const dirty = { dirty: true, armed: false };
    expect(reduceDirtyGuard(dirty, { type: 'dirty-change', dirty: true })).toEqual(dirty);
  });

  test('first click arms (relabel), second click flows through the view to reset', () => {
    const dirty = { dirty: true, armed: false };
    expect(reduceDirtyGuard(dirty, { type: 'arm' })).toEqual({ dirty: true, armed: true });
  });

  test('the arm window elapsing disarms back to dirty', () => {
    const armed = { dirty: true, armed: true };
    expect(reduceDirtyGuard(armed, { type: 'disarm' })).toEqual({ dirty: true, armed: false });
    expect(reduceDirtyGuard({ dirty: true, armed: false }, { type: 'disarm' })).toEqual({
      dirty: true,
      armed: false
    });
  });

  test('edits while armed stay armed (the window governs the reset)', () => {
    const armed = { dirty: true, armed: true };
    expect(reduceDirtyGuard(armed, { type: 'dirty-change', dirty: true })).toEqual(armed);
  });

  test('a revert while dirty or armed lands pristine', () => {
    expect(reduceDirtyGuard({ dirty: true, armed: false }, { type: 'dirty-change', dirty: false })).toEqual(
      DIRTY_GUARD_CLEAN
    );
    expect(reduceDirtyGuard({ dirty: true, armed: true }, { type: 'dirty-change', dirty: false })).toEqual(
      DIRTY_GUARD_CLEAN
    );
  });

  test('reset (reload / load / successful save) always lands pristine', () => {
    for (const state of [DIRTY_GUARD_CLEAN, { dirty: true, armed: false }, { dirty: true, armed: true }]) {
      expect(reduceDirtyGuard(state, { type: 'reset' })).toEqual(DIRTY_GUARD_CLEAN);
    }
  });
});