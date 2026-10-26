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

/** Validate one condition value; returns violation strings. */
export function validateRuleConditionKeys(rules: BotRule[] | null | undefined, indexPath = 'rules'): string[] {
  const details: string[] = [];
  if (!Array.isArray(rules)) {
    details.push(`${indexPath} deve ser uma lista`);
    return details;
  }
  rules.forEach((rule, index) => {
    if (!rule || typeof rule !== 'object') {
      details.push(`${indexPath}[${index}] deve ser um objeto`);
      return;
    }
    if (!isNonEmptyStr(rule.name)) details.push(`regra ${index}: 'name' é obrigatório`);
    if (!Array.isArray(rule.actions) || rule.actions.length === 0 || !isStrList(rule.actions, false)) {
      details.push(`regra ${rule.name ?? index}: 'actions' deve ser uma lista de strings não vazias`);
    }
    const condition = rule.condition;
    if (!condition || typeof condition !== 'object' || Array.isArray(condition)) {
      details.push(`regra ${rule.name ?? index}: 'condition' deve ser um objeto`);
      return;
    }
    for (const key of Object.keys(condition)) {
      if (!KNOWN_RULE_FIELDS.includes(key as (typeof KNOWN_RULE_FIELDS)[number])) {
        details.push(
          `regra '${rule.name ?? index}': campo de condição desconhecido '${key}' (permitidos: ${KNOWN_RULE_FIELDS.join(', ')})`
        );
      }
      const value = condition[key];
      if (!(typeof value === 'string' || isStrList(value, false))) {
        details.push(`regra '${rule.name ?? index}': valores de '${key}' devem ser string ou lista de strings`);
      }
    }
  });
  return details;
}

export function validateActionEntries(actions: ActionEntry[] | null | undefined): string[] {
  const details: string[] = [];
  if (!Array.isArray(actions)) {
    details.push('actions deve ser uma lista');
    return details;
  }
  const seen: string[] = [];
  actions.forEach((action, index) => {
    if (!isNonEmptyStr(action?.id)) {
      details.push(`actions[${index}].id é obrigatório`);
    } else if (seen.includes(action.id)) {
      details.push(`id de ação duplicado: '${action.id}'`);
    } else {
      seen.push(action.id);
    }
    if (!ACTION_TYPES.includes(action?.type)) {
      details.push(`actions[${index}].type deve ser 'builtin' ou 'command'`);
    }
    if (typeof action?.enabled !== 'boolean') {
      details.push(`actions[${index}].enabled deve ser booleano`);
    }
    if (typeof action?.match_hint !== 'string') {
      details.push(`actions[${index}].match_hint deve ser string`);
    }
  });
  return details;
}

/** Full-config validation (mirrors the server; empty array == valid). */
export function validateConfigDraft(config: unknown): string[] {
  const details: string[] = [];
  if (!config || typeof config !== 'object') {
    return ['config deve ser um objeto'];
  }
  const config0 = config as BotConfig;

  if (!isNumber(config0.poll_interval as number) || (config0.poll_interval as number) <= 0) {
    details.push("'poll_interval' deve ser um número > 0");
  }

  const ollama = config0.ollama;
  if (!ollama || typeof ollama !== 'object') {
    details.push("seção 'ollama' é obrigatória");
  } else {
    if (!isNonEmptyStr(ollama.url)) details.push("'ollama.url' deve ser uma string não vazia");
    if (!isNonEmptyStr(ollama.model)) details.push("'ollama.model' deve ser uma string não vazia");
    if (!isNumber(ollama.timeout) || ollama.timeout <= 0) {
      details.push("'ollama.timeout' deve ser um número > 0");
    }
  }

  const routerCfg = config0.router;
  if (!routerCfg || typeof routerCfg !== 'object') {
    details.push("seção 'router' é obrigatória");
  } else {
    if (!isNumber(routerCfg.min_confidence) || routerCfg.min_confidence < 0 || routerCfg.min_confidence > 1) {
      details.push("'router.min_confidence' deve ser um número entre 0 e 1");
    }
    if (!isNumber(routerCfg.timeout) || routerCfg.timeout <= 0) {
      details.push("'router.timeout' deve ser um número > 0");
    }
  }

  const telegram = config0.telegram;
  if (telegram && typeof telegram === 'object') {
    if (telegram.token !== '') details.push("'telegram.token' deve ser vazio (env-only)");
    if (telegram.chat_id !== '') details.push("'telegram.chat_id' deve ser vazio (env-only)");
  }

  details.push(...validateActionEntries(config0.actions));
  details.push(...validateRuleConditionKeys(config0.rules));

  const finance = config0.finance;
  if (finance && typeof finance === 'object') {
    if (typeof finance.enabled !== 'boolean') details.push("'finance.enabled' deve ser booleano");
    if (typeof finance.ledger_db !== 'string') details.push("'finance.ledger_db' deve ser string");
    if (!isStrList(finance.bank_apps)) details.push("'finance.bank_apps' deve ser uma lista de strings");
    if (typeof finance.telegram_summary !== 'boolean') {
      details.push("'finance.telegram_summary' deve ser booleano");
    }
    if (!isStrList(finance.categories, false) || finance.categories.length === 0) {
      details.push("'finance.categories' deve ser uma lista de strings não vazias");
    }
  }

  return details;
}