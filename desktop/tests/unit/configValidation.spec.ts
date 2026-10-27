import { describe, expect, test } from 'vitest';

import {
  KNOWN_RULE_FIELDS,
  operationallyRelevantChange,
  splitDetailsByField,
  validateActionEntries,
  validateConfigDraft,
  validateDraftFields,
  validateRuleConditionKeys
} from '../../src/services/configValidation';
import type { ActionEntry, BotConfig, BotRule } from '../../src/types/bot';

const KNOWN = [...KNOWN_RULE_FIELDS];

function validConfig(overrides: Partial<BotConfig> = {}): BotConfig {
  return {
    poll_interval: 2,
    state_file: '.bot_state.json',
    log_file: 'logs/events.jsonl',
    ollama: { url: 'http://127.0.0.1:11434/api/chat', model: 'glm-5.3-flash:cloud', timeout: 30 },
    telegram: { token: '', chat_id: '' },
    router: { min_confidence: 0.5, timeout: 15 },
    actions: [
      { id: 'log', match_hint: 'registrar', type: 'builtin', enabled: true, config: {} },
      { id: 'example_command', match_hint: 'cmd', type: 'command', enabled: false, config: { command: '/x' } }
    ],
    rules: [
      { name: 'high_urgency', condition: { urgency: ['high'] }, actions: ['log', 'telegram'] }
    ],
    finance: {
      enabled: true,
      ledger_db: 'finance.db',
      bank_apps: [],
      telegram_summary: false,
      categories: ['alimentacao', 'outros']
    },
    ...overrides
  };
}

describe('validateConfigDraft mirrors the server rules', () => {
  test('a valid default config has no violations', () => {
    expect(validateConfigDraft(validConfig())).toEqual([]);
  });

  test('rejects wrong section types', () => {
    const details = validateConfigDraft(
      validConfig({
        ollama: 3 as never,
        router: 'x' as never,
        actions: {} as never,
        rules: undefined as never,
        telegram: 1 as never,
        finance: 2 as never
      })
    );
    for (const fragment of ['ollama', 'router', 'actions', 'rules', 'telegram', 'finance']) {
      expect(details.some((d) => d.includes(fragment))).toBe(true);
    }
  });

  test('numeric ranges: poll_interval>0, timeouts>0, 0<=min_confidence<=1', () => {
    const details = validateConfigDraft(
      validConfig({
        poll_interval: 0,
        ollama: { url: 'u', model: 'm', timeout: -5 },
        router: { min_confidence: 1.5, timeout: 0 }
      })
    );
    expect(details.join('\n')).toContain("'poll_interval'");
    expect(details.join('\n')).toContain("'ollama.timeout'");
    expect(details.join('\n')).toContain("'router.min_confidence'");
    expect(details.join('\n')).toContain("'router.timeout'");
  });

  test('min_confidence exactly 0 and 1 are valid', () => {
    expect(
      validateConfigDraft(validConfig({ router: { min_confidence: 0, timeout: 1 } }))
    ).toEqual([]);
    expect(
      validateConfigDraft(validConfig({ router: { min_confidence: 1, timeout: 1 } }))
    ).toEqual([]);
  });

  test('rejects bool masquerading as number (poll_interval: true)', () => {
    const details = validateConfigDraft(validConfig({ poll_interval: true as never }));
    expect(details.join('\n')).toContain('poll_interval');
  });

  test('rejects non-empty telegram token/chat_id (env-only)', () => {
    const details = validateConfigDraft(
      validConfig({ telegram: { token: 'SECRET', chat_id: '123' } })
    );
    expect(details.some((d) => d.includes('telegram.token'))).toBe(true);
    expect(details.some((d) => d.includes('telegram.chat_id'))).toBe(true);
  });

  test('masked values from GET are also rejected client-side', () => {
    const details = validateConfigDraft(
      validConfig({ telegram: { token: '********', chat_id: '********' } })
    );
    expect(details.length).toBeGreaterThanOrEqual(2);
  });
});

describe('action registry checks', () => {
  test('duplicate ids rejected', () => {
    const actions = [
      { id: 'log', match_hint: 'x', type: 'builtin' as const, enabled: true, config: {} },
      { id: 'log', match_hint: 'y', type: 'builtin' as const, enabled: true, config: {} }
    ];
    const details = validateActionEntries(actions);
    expect(details.join('\n')).toContain("duplicate action id: 'log'");
  });

  test('unknown action type rejected', () => {
    const actions: ActionEntry[] = [
      { id: 'webhook', match_hint: '', type: 'shell' as never, enabled: true, config: {} }
    ];
    const details = validateActionEntries(actions);
    expect(details.join('\n')).toContain("'builtin' or 'command'");
  });

  test('empty id and non-bool enabled rejected', () => {
    const details = validateActionEntries([
      { id: '', match_hint: '', type: 'builtin', enabled: true, config: {} },
      { id: 'log', match_hint: '', type: 'builtin', enabled: false, config: {} },
      { id: 'telegram', match_hint: '', type: 'builtin', enabled: 'true' as never, config: {} }
    ]);
    expect(details.join('\n')).toContain('id is required');
    expect(details.join('\n')).toContain('enabled must be a boolean');
  });

  test('command actions are legal', () => {
    const details = validateActionEntries([
      { id: 'example_command', match_hint: 'cmd', type: 'command', enabled: false, config: { command: '/x' } }
    ]);
    expect(details).toEqual([]);
  });
});

describe('rule condition checks', () => {
  test('unknown condition fields rejected with the known list', () => {
    const rules: BotRule[] = [
      { name: 'r1', condition: { notAField: ['x'] }, actions: ['log'] }
    ];
    const details = validateRuleConditionKeys(rules);
    expect(details.join('\n')).toContain("unknown condition field 'notAField'");
    expect(details.join('\n')).toContain(KNOWN.join(', '));
  });

  test('all known fields accepted; scalar and list values allowed', () => {
    const condition: Record<string, string | string[]> = {
      app: 'com.apple.mail',
      title: ['a', 'b'],
      subtitle: 's',
      body: 'b',
      category: ['finance'],
      urgency: ['high'],
      summary: 'resumo'
    };
    expect(validateRuleConditionKeys([{ name: 'all_fields', condition, actions: ['log'] }])).toEqual([]);
  });

  test('empty actions list is legal (server-side); scalar condition values validated', () => {
    // condition value list may be empty (matches nothing, allowed server-side)
    expect(
      validateRuleConditionKeys([{ name: 'r', condition: { urgency: [] }, actions: ['log'] }])
    ).toEqual([]);
    const details = validateRuleConditionKeys([
      { name: 'r', condition: { urgency: 3 as never }, actions: [] }
    ]);
    const joined = details.join('\n');
    expect(joined).toContain("condition values for 'urgency'");
    expect(joined).toContain("'actions' must be a list");
  });

  test('missing name/condition rejected', () => {
    const details = validateRuleConditionKeys([{} as unknown as BotRule]);
    expect(details.join('\n')).toContain("'name'");
    expect(details.join('\n')).toContain("'condition'");
  });

  test('non-list rules rejected', () => {
    expect(validateRuleConditionKeys(null).length).toBeGreaterThan(0);
    expect(validateRuleConditionKeys({} as never).length).toBeGreaterThan(0);
  });
});

describe('full draft edge cases', () => {
  test('empty rules list is valid (Router v2 LLM handles everything)', () => {
    expect(validateConfigDraft(validConfig({ rules: [] }))).toEqual([]);
  });

  test('non-object drafts rejected with a single message', () => {
    expect(validateConfigDraft(null)).toEqual(['config must be an object']);
    expect(validateConfigDraft('str')).toEqual(['config must be an object']);
  });

  test('no PT-BR wording leaks into client mirror details', () => {
    const details = validateConfigDraft(
      validConfig({ poll_interval: 0, router: { min_confidence: 5, timeout: 0 }, ollama: { url: '', model: '', timeout: 0 } })
    );
    const joined = details.join('\n');
    for (const pt of ['deve ser', 'não', 'obrigatório', 'desconhecido', 'inválida']) {
      expect(joined).not.toContain(pt);
    }
  });
});

describe('validateDraftFields (loop2 iterC inline rules)', () => {
  const valid = {
    ollamaUrl: 'http://127.0.0.1:11434/api/chat',
    ollamaModel: 'llama3',
    ollamaTimeout: '30',
    minConfidence: '0.5',
    pollInterval: '2'
  };

  test('all valid entries pass', () => {
    expect(validateDraftFields(valid)).toEqual({});
  });

  test('the four mission messages surface verbatim (EN)', () => {
    const errors = validateDraftFields({
      ollamaUrl: 'ftp://x',
      ollamaModel: ' ',
      ollamaTimeout: '0',
      minConfidence: '5',
      pollInterval: '0'
    });
    expect(errors.ollamaUrl).toBe('URL must start with http:// or https://');
    expect(errors.ollamaModel).toBe('model must not be empty');
    expect(errors.ollamaTimeout).toBe('timeout must be greater than 0');
    expect(errors.minConfidence).toBe('min_confidence must be between 0 and 1');
    expect(errors.pollInterval).toBe('poll_interval must be greater than 0');
  });

  test('url rule: empty and bad-scheme share the one message; https passes', () => {
    expect(validateDraftFields({ ...valid, ollamaUrl: '' }).ollamaUrl).toBe('URL must start with http:// or https://');
    expect(validateDraftFields({ ...valid, ollamaUrl: '127.0.0.1:11434' }).ollamaUrl).toBe('URL must start with http:// or https://');
    expect(validateDraftFields({ ...valid, ollamaUrl: 'ftp://x' }).ollamaUrl).toBe('URL must start with http:// or https://');
    expect(validateDraftFields({ ...valid, ollamaUrl: 'https://ollama.lan' }).ollamaUrl).toBeUndefined();
    expect(validateDraftFields({ ...valid, ollamaUrl: 'HTTP://OLLAMA.LAN' }).ollamaUrl).toBeUndefined();
  });

  test('bounds: timeout/min_confidence/poll_interval edge values', () => {
    expect(validateDraftFields({ ...valid, ollamaTimeout: 'abc' }).ollamaTimeout).toBeDefined();
    expect(validateDraftFields({ ...valid, ollamaTimeout: '-1' }).ollamaTimeout).toBeDefined();
    expect(validateDraftFields({ ...valid, ollamaTimeout: '0.5' }).ollamaTimeout).toBeUndefined();
    expect(validateDraftFields({ ...valid, minConfidence: '-0.1' }).minConfidence).toBeDefined();
    expect(validateDraftFields({ ...valid, minConfidence: '1.5' }).minConfidence).toBeDefined();
    expect(validateDraftFields({ ...valid, minConfidence: '0' }).minConfidence).toBeUndefined();
    expect(validateDraftFields({ ...valid, minConfidence: '1' }).minConfidence).toBeUndefined();
    expect(validateDraftFields({ ...valid, pollInterval: 'abc' }).pollInterval).toBeDefined();
    expect(validateDraftFields({ ...valid, pollInterval: '0.0001' }).pollInterval).toBeUndefined();
  });
});

describe('splitDetailsByField (save response -> inline field mapping)', () => {
  test('maps server/PT field details to their field with English rule text', () => {
    const { fieldErrors, unmapped } = splitDetailsByField([
      "'ollama.url' deve ser uma string não vazia",
      "'router.min_confidence' deve ser um número entre 0 e 1",
      "'poll_interval' deve ser um número > 0",
      "'ollama.timeout' deve ser um número > 0",
      "'ollama.model' deve ser uma string não vazia"
    ]);
    expect(fieldErrors.ollamaUrl).toBe('URL must start with http:// or https://');
    expect(fieldErrors.ollamaModel).toBe('model must not be empty');
    expect(fieldErrors.ollamaTimeout).toBe('timeout must be greater than 0');
    expect(fieldErrors.minConfidence).toBe('min_confidence must be between 0 and 1');
    expect(fieldErrors.pollInterval).toBe('poll_interval must be greater than 0');
    expect(unmapped).toEqual([]);
  });

  test('mapped field errors never carry the PT-BR server wording', () => {
    const { fieldErrors, unmapped } = splitDetailsByField([
      "'router.min_confidence' deve ser um número entre 0 e 1"
    ]);
    expect(fieldErrors.minConfidence).not.toContain('deve ser');
    expect(unmapped).toEqual([]);
  });

  test('keeps non-field details in the unmapped list (view shows a generic line)', () => {
    const { fieldErrors, unmapped } = splitDetailsByField([
      "regra 'x': campo de condição desconhecido 'weird' (permitidos: app)",
      "'ollama.model' deve ser uma string não vazia"
    ]);
    expect(fieldErrors.ollamaModel).toBe('model must not be empty');
    expect(unmapped).toHaveLength(1);
  });

  test('maps the bare 400 error text when details are absent', () => {
    const { fieldErrors } = splitDetailsByField(["config inválida: 'poll_interval' deve ser um número > 0"]);
    expect(fieldErrors.pollInterval).toBe('poll_interval must be greater than 0');
  });

  test('first field detail wins on duplicates', () => {
    const { fieldErrors } = splitDetailsByField([
      "'ollama.url' deve ser uma string não vazia",
      "'ollama.url' outro detalhe"
    ]);
    expect(fieldErrors.ollamaUrl).toBe('URL must start with http:// or https://');
  });
});

describe('operationallyRelevantChange (post-save restart banner gate)', () => {
  const base = {
    ollamaUrl: 'http://127.0.0.1:11434/api/chat',
    ollamaModel: 'm',
    ollamaTimeout: '30',
    minConfidence: '0.5',
    pollInterval: '2'
  };

  test('identical drafts mean no restart needed', () => {
    expect(operationallyRelevantChange(base, base)).toBe(false);
  });

  test('each of the five fields triggers the change', () => {
    const deltas: Array<[keyof typeof base, string]> = [
      ['ollamaUrl', 'http://other:11434'],
      ['ollamaModel', 'other-model'],
      ['ollamaTimeout', '60'],
      ['minConfidence', '0.9'],
      ['pollInterval', '5']
    ];
    for (const [key, value] of deltas) {
      const next = { ...base, [key]: value };
      expect(operationallyRelevantChange(base, next)).toBe(true);
      expect(operationallyRelevantChange(next, base)).toBe(true);
    }
  });

  test('numeric formatting differences are not a change', () => {
    expect(
      operationallyRelevantChange(base, {
        ...base,
        minConfidence: '.5',
        pollInterval: '2.0',
        ollamaTimeout: '30.00'
      })
    ).toBe(false);
  });

  test('whitespace-only url/model edits are not a change', () => {
    expect(
      operationallyRelevantChange(base, {
        ...base,
        ollamaUrl: '  http://127.0.0.1:11434/api/chat  ',
        ollamaModel: ' m '
      })
    ).toBe(false);
  });
});