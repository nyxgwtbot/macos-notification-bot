/**
 * Client-side validation mirroring notifybot/api.py validate_config_payload
 * (POST /api/config rules) so the Config screen shows problems before the
 * round-trip. The server re-validates; its details[] win on conflict.
 *
 * tests: tests/unit/configValidation.spec.ts
 */

import type { ActionEntry, BotConfig, BotRule } from '@/types/bot';

export const KNOWN_RULE_FIELDS = ['app', 'title', 'subtitle', 'body', 'category', 'urgency', 'summary'] as const;

const ACTION_TYPES = ['builtin', 'command'];

/** The five editable structured config fields (ConfigView, loop2 iterB). */
export type ConfigFieldKey = 'ollamaUrl' | 'ollamaModel' | 'ollamaTimeout' | 'minConfidence' | 'pollInterval';

export interface ConfigDraftFields {
  ollamaUrl: string;
  ollamaModel: string;
  ollamaTimeout: string;
  minConfidence: string;
  pollInterval: string;
}

/** Canonical English rule text per field (loop2 iterC copy; server details
 * are PT-BR and must never surface verbatim — this table is also the EN
 * mapping target for a 400 response's field details). */
const FIELD_RULE_TEXT: Record<ConfigFieldKey, string> = {
  ollamaUrl: 'URL must start with http:// or https://',
  ollamaModel: 'model must not be empty',
  ollamaTimeout: 'timeout must be greater than 0',
  minConfidence: 'min_confidence must be between 0 and 1',
  pollInterval: 'poll_interval must be greater than 0'
};

/** Server detail snippets (notifybot/api.py validate_config_payload wording)
 * that identify exactly one field. */
const FIELD_DETAIL_PATTERNS: ReadonlyArray<{ key: ConfigFieldKey; re: RegExp }> = [
  { key: 'ollamaUrl', re: /'ollama\.url'/ },
  { key: 'ollamaModel', re: /'ollama\.model'/ },
  { key: 'ollamaTimeout', re: /'ollama\.timeout'/ },
  { key: 'minConfidence', re: /'router\.min_confidence'/ },
  { key: 'pollInterval', re: /'poll_interval'/ }
];

function parseFiniteNumber(text: string): number | null {
  if (text.trim() === '') return null;
  const value = Number(text);
  return Number.isFinite(value) ? value : null;
}

/** Per-field rule check for the structured editor inputs (client-side rules:
 * url http(s), model non-empty, timeout > 0, min_confidence 0–1,
 * poll_interval > 0 — the server does not enforce the scheme). */
export function validateDraftFields(fields: ConfigDraftFields): Partial<Record<ConfigFieldKey, string>> {
  const errors: Partial<Record<ConfigFieldKey, string>> = {};

  // one message for the whole url rule: an empty value does not match
  // ^https?:// either, so empty and bad-scheme share the copy
  if (!/^https?:\/\//i.test(fields.ollamaUrl.trim())) {
    errors.ollamaUrl = FIELD_RULE_TEXT.ollamaUrl;
  }

  if (fields.ollamaModel.trim() === '') errors.ollamaModel = FIELD_RULE_TEXT.ollamaModel;

  const timeout = parseFiniteNumber(fields.ollamaTimeout);
  if (timeout === null || timeout <= 0) errors.ollamaTimeout = FIELD_RULE_TEXT.ollamaTimeout;

  const minConfidence = parseFiniteNumber(fields.minConfidence);
  if (minConfidence === null || minConfidence < 0 || minConfidence > 1) {
    errors.minConfidence = FIELD_RULE_TEXT.minConfidence;
  }

  const pollInterval = parseFiniteNumber(fields.pollInterval);
  if (pollInterval === null || pollInterval <= 0) errors.pollInterval = FIELD_RULE_TEXT.pollInterval;

  return errors;
}

/**
 * Restart-to-apply gate (loop2 iterC): true when any ollama/router/poll value
 * differs between the last-loaded draft and the edited draft — the daemon
 * reads the config only at boot, so the banner shows only for these five
 * fields. Numeric fields compare as numbers ('0.5' == '.5'), url/model as
 * trimmed strings (whitespace-only edits are not a change). */
export function operationallyRelevantChange(previous: ConfigDraftFields, next: ConfigDraftFields): boolean {
  const sameNumericText = (a: string, b: string): boolean => {
    const an = parseFiniteNumber(a);
    const bn = parseFiniteNumber(b);
    if (an !== null && bn !== null) return an === bn;
    return a.trim() === b.trim();
  };
  if (!sameNumericText(previous.ollamaTimeout, next.ollamaTimeout)) return true;
  if (!sameNumericText(previous.minConfidence, next.minConfidence)) return true;
  if (!sameNumericText(previous.pollInterval, next.pollInterval)) return true;
  if (previous.ollamaUrl.trim() !== next.ollamaUrl.trim()) return true;
  if (previous.ollamaModel.trim() !== next.ollamaModel.trim()) return true;
  return false;
}

/**
 * Map a save response's details[] (or a 400 error text) to the specific
 * field: each detail identifies at most one field (matched by the quoted
 * '<path>' prefix, independent of detail language). English canonical texts
 * are used for the inline message; unmatched details never surface verbatim
 * (the view renders a single muted generic line for them). */
export function splitDetailsByField(details: string[]): {
  fieldErrors: Partial<Record<ConfigFieldKey, string>>;
  unmapped: string[];
} {
  const fieldErrors: Partial<Record<ConfigFieldKey, string>> = {};
  const unmapped: string[] = [];
  for (const detail of details) {
    const match = FIELD_DETAIL_PATTERNS.find((entry) => entry.re.test(detail));
    if (match && fieldErrors[match.key] === undefined) {
      fieldErrors[match.key] = FIELD_RULE_TEXT[match.key];
    } else if (!match) {
      unmapped.push(detail);
    }
  }
  return { fieldErrors, unmapped };
}

function isNumber(value: unknown): value is number {
  return typeof value === 'number' && Number.isFinite(value);
}

function isNonEmptyStr(value: unknown): boolean {
  return typeof value === 'string' && value.trim() !== '';
}

function isStrList(value: unknown, allowEmpty = true): boolean {
  if (!Array.isArray(value)) return false;
  return value.every((item) => typeof item === 'string' && (allowEmpty || item.trim() !== ''));
}

/** Validate one condition value; returns violation strings (English: these
 * surface in the raw-JSON editor and the client banner — api.py's PT-BR
 * details stay server-side). Path tokens ('rules[0]', 'name'…) are kept so
 * splitDetailsByField keeps matching by the quoted path. */
export function validateRuleConditionKeys(rules: BotRule[] | null | undefined, indexPath = 'rules'): string[] {
  const details: string[] = [];
  if (!Array.isArray(rules)) {
    details.push(`${indexPath} must be a list`);
    return details;
  }
  rules.forEach((rule, index) => {
    if (!rule || typeof rule !== 'object') {
      details.push(`${indexPath}[${index}] must be an object`);
      return;
    }
    if (!isNonEmptyStr(rule.name)) details.push(`rule ${index}: 'name' is required`);
    if (!Array.isArray(rule.actions) || rule.actions.length === 0 || !isStrList(rule.actions, false)) {
      details.push(`rule ${rule.name ?? index}: 'actions' must be a list of non-empty strings`);
    }
    const condition = rule.condition;
    if (!condition || typeof condition !== 'object' || Array.isArray(condition)) {
      details.push(`rule ${rule.name ?? index}: 'condition' must be an object`);
      return;
    }
    for (const key of Object.keys(condition)) {
      if (!KNOWN_RULE_FIELDS.includes(key as (typeof KNOWN_RULE_FIELDS)[number])) {
        details.push(
          `rule '${rule.name ?? index}': unknown condition field '${key}' (allowed: ${KNOWN_RULE_FIELDS.join(', ')})`
        );
      }
      const value = condition[key];
      if (!(typeof value === 'string' || isStrList(value))) {
        details.push(`rule '${rule.name ?? index}': condition values for '${key}' must be string or list of strings`);
      }
    }
  });
  return details;
}

/* ========================================
   R8 ACTION ROWS — the Settings Actions editor parity: an empty command on a
   type:'command' action is a per-row field error (index-keyed so the view can
   bind the standard .nb-input-bad box + inline message straight from the map)
   that joins the draft validation and blocks the save. Builtin entries carry
   no command and always pass.
   ======================================== */

/** Canonical English message for the empty-command field error. */
export const ACTION_COMMAND_REQUIRED_TEXT = 'command is required for command actions';

/** The one draft-shaped field rule for the Actions rows (empty command when
 * type is 'command'). Mirrors the shape of validateDraftFields: a map of
 * index -> message, empty when every row is valid. */
export function validateDraftActionCommands(
  actions: ReadonlyArray<{ type: 'builtin' | 'command'; command: string }>
): Record<number, string> {
  const errors: Record<number, string> = {};
  actions.forEach((action, index) => {
    if (action.type === 'command' && action.command.trim() === '') {
      errors[index] = ACTION_COMMAND_REQUIRED_TEXT;
    }
  });
  return errors;
}

export function validateActionEntries(actions: ActionEntry[] | null | undefined): string[] {
  const details: string[] = [];
  if (!Array.isArray(actions)) {
    details.push('actions must be a list');
    return details;
  }
  const seen: string[] = [];
  actions.forEach((action, index) => {
    if (!isNonEmptyStr(action?.id)) {
      details.push(`actions[${index}].id is required`);
    } else if (seen.includes(action.id)) {
      details.push(`duplicate action id: '${action.id}'`);
    } else {
      seen.push(action.id);
    }
    if (!ACTION_TYPES.includes(action?.type)) {
      details.push(`actions[${index}].type must be 'builtin' or 'command'`);
    }
    if (typeof action?.enabled !== 'boolean') {
      details.push(`actions[${index}].enabled must be a boolean`);
    }
    if (typeof action?.match_hint !== 'string') {
      details.push(`actions[${index}].match_hint must be a string`);
    }
  });
  return details;
}

/** Full-config validation (mirrors the server; empty array == valid). All
 * detail copy is English — see validateRuleConditionKeys above. */
export function validateConfigDraft(config: unknown): string[] {
  const details: string[] = [];
  if (!config || typeof config !== 'object') {
    return ['config must be an object'];
  }
  const config0 = config as BotConfig;

  for (const section of ['ollama', 'telegram', 'router', 'finance'] as const) {
    const value = config0[section];
    if (!value || typeof value !== 'object' || Array.isArray(value)) {
      details.push(`section '${section}' must be an object`);
    }
  }
  for (const section of ['actions', 'rules'] as const) {
    if (!Array.isArray(config0[section])) {
      details.push(`section '${section}' must be a list`);
    }
  }

  if (!isNumber(config0.poll_interval as number) || (config0.poll_interval as number) <= 0) {
    details.push("'poll_interval' must be a number > 0");
  }

  const ollama = config0.ollama;
  if (ollama && typeof ollama === 'object') {
    if (!isNonEmptyStr(ollama.url)) details.push("'ollama.url' must be a non-empty string");
    if (!isNonEmptyStr(ollama.model)) details.push("'ollama.model' must be a non-empty string");
    if (!isNumber(ollama.timeout) || ollama.timeout <= 0) {
      details.push("'ollama.timeout' must be a number > 0");
    }
  }

  const routerCfg = config0.router;
  if (routerCfg && typeof routerCfg === 'object') {
    if (!isNumber(routerCfg.min_confidence) || routerCfg.min_confidence < 0 || routerCfg.min_confidence > 1) {
      details.push("'router.min_confidence' must be a number between 0 and 1");
    }
    if (!isNumber(routerCfg.timeout) || routerCfg.timeout <= 0) {
      details.push("'router.timeout' must be a number > 0");
    }
  }

  const telegram = config0.telegram;
  if (telegram && typeof telegram === 'object') {
    if (telegram.token !== '') details.push("'telegram.token' must be empty (env-only)");
    if (telegram.chat_id !== '') details.push("'telegram.chat_id' must be empty (env-only)");
  }

  details.push(...validateActionEntries(config0.actions));
  details.push(...validateRuleConditionKeys(config0.rules));

  const finance = config0.finance;
  if (finance && typeof finance === 'object') {
    if (typeof finance.enabled !== 'boolean') details.push("'finance.enabled' must be a boolean");
    if (typeof finance.ledger_db !== 'string') details.push("'finance.ledger_db' must be a string");
    if (!isStrList(finance.bank_apps)) details.push("'finance.bank_apps' must be a list of strings");
    if (typeof finance.telegram_summary !== 'boolean') {
      details.push("'finance.telegram_summary' must be a boolean");
    }
    if (!isStrList(finance.categories) || finance.categories.length === 0) {
      details.push("'finance.categories' must be a list of non-empty strings");
    }
  }

  return details;
}