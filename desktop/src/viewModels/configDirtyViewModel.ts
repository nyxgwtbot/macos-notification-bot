/**
 * Config dirty-guard view model (R7): pristine-vs-dirty tracking for the
 * Settings draft, the discard double-arm reducer for the header reload
 * button, and the save/restart SR announce strings. Pure functions only
 * (tests: tests/unit/configDirtyViewModel.spec.ts).
 *
 * The view snapshots the draft at load (buildDraft) and after a successful
 * save; every keystroke/toggle re-serializes the draft against the snapshot
 * (canonical form: whitespace-stable fields join, stable-ordered actions,
 * and rules reduced to compact JSON from either the structured editor or a
 * parseable raw editor — an unparseable rawRules is dirty by definition,
 * since the load-time rawRules always parses).
 */

import type { BotRule } from '@/types/bot';

/* ========================================
   SR ANNOUNCE STRINGS — one visually-hidden polite region drives these from
   save()/onSaveRestartClick(). Exact strings are asserted in the spec.
   ======================================== */

export const SETTINGS_SAVED_ANNOUNCE = 'Settings saved.';

export const SAVE_FAILED_ANNOUNCE = 'Save failed.';

export const RESTART_APPLY_ANNOUNCE = 'Restart scheduled to apply.';

/* ========================================
   DIRTY SNAPSHOT — canonical serialization of the tracked draft. The view's
   ConfigDraft is structurally assignable to DirtyTrackedDraft (read-only
   view; the VM never mutates it).
   ======================================== */

export interface ActionDraftLike {
  id: string;
  type: 'builtin' | 'command';
  enabled: boolean;
  match_hint: string;
  command: string;
}

export interface DirtyTrackedDraft {
  pollInterval: string;
  ollamaUrl: string;
  ollamaModel: string;
  ollamaTimeout: string;
  minConfidence: string;
  actions: ReadonlyArray<ActionDraftLike>;
  rules: ReadonlyArray<BotRule>;
  mode: 'structured' | 'raw';
  rawRules: string;
}

/** Rules reduced to compact JSON from either source; a raw editor state
 * whose text does not parse cannot equal a snapshot (both were built from
 * parseable JSON at load/save time). */
function rulesCanonical(draft: DirtyTrackedDraft): string {
  if (draft.mode === 'raw') {
    try {
      return JSON.stringify(JSON.parse(draft.rawRules) as unknown);
    } catch {
      // 'invalid' prefix keeps a malformed raw text from colliding with any
      // valid-JSON canonical form
      return 'invalid\u0000' + draft.rawRules;
    }
  }
  return JSON.stringify(draft.rules);
}

export type ConfigDirtySnapshot = string;

export function configDraftSnapshot(draft: DirtyTrackedDraft): ConfigDirtySnapshot {
  return [
    'v1',
    draft.ollamaUrl,
    draft.ollamaModel,
    draft.ollamaTimeout,
    draft.minConfidence,
    draft.pollInterval,
    JSON.stringify(draft.actions),
    rulesCanonical(draft)
  ].join('\u0000');
}

/** Null snapshot = not yet loaded (no reference) → never dirty. */
export function draftIsDirty(snapshot: ConfigDirtySnapshot | null, draft: DirtyTrackedDraft): boolean {
  return snapshot !== null && snapshot !== configDraftSnapshot(draft);
}

/* ========================================
   DIRTY-GUARD REDUCER — state machine behind the header reload button:
   pristine ("Reload settings", disabled) -> dirty ("Discard edits?", first
   click arms) -> armed (tinted label, second click performs the discard
   reload). Mirrors the restart double-arm; the 3s auto-disarm timer lives
   in the view — it dispatches 'disarm' when the window elapses.
   ======================================== */

export interface DirtyGuardState {
  dirty: boolean;
  armed: boolean;
}

export type DirtyGuardEvent =
  /** The snapshot compare flipped — carries the current dirty boolean. */
  | { type: 'dirty-change'; dirty: boolean }
  /** First click on the reload button (only effective while dirty). */
  | { type: 'arm' }
  /** Arm window elapsed / a fresh edit reverted the draft (view-driven). */
  | { type: 'disarm' }
  /** Snapshot refreshed (reload, load, successful save) — pristine again. */
  | { type: 'reset' };

export const DIRTY_GUARD_CLEAN: DirtyGuardState = { dirty: false, armed: false };

export function reduceDirtyGuard(state: DirtyGuardState, event: DirtyGuardEvent): DirtyGuardState {
  switch (event.type) {
    case 'dirty-change':
      // an edit while armed stays armed (the arm window governs the reset);
      // a revert drops the arm too — no edits left to discard
      return event.dirty ? (state.armed ? state : { dirty: true, armed: false }) : DIRTY_GUARD_CLEAN;
    case 'arm':
      // pristine never arms (the button is disabled there anyway)
      return state.dirty ? { dirty: true, armed: true } : state;
    case 'disarm':
      return state.armed ? { ...state, armed: false } : state;
    case 'reset':
      return DIRTY_GUARD_CLEAN;
  }
}