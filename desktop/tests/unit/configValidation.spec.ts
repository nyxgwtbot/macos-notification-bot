import { describe, expect, test } from 'vitest';

import {
  KNOWN_RULE_FIELDS,
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
    expect(details.join('\n')).toContain("duplicado: 'log'");
  });

  test('unknown action type rejected', () => {
    const actions: ActionEntry[] = [
      { id: 'webhook', match_hint: '', type: 'shell' as never, enabled: true, config: {} }
    ];
    const details = validateActionEntries(actions);
    expect(details.join('\n')).toContain("'builtin' ou 'command'");
  });

  test('empty id and non-bool enabled rejected', () => {
    const details = validateActionEntries([
      { id: '', match_hint: '', type: 'builtin', enabled: true, config: {} },
      { id: 'log', match_hint: '', type: 'builtin', enabled: false, config: {} },
      { id: 'telegram', match_hint: '', type: 'builtin', enabled: 'true' as never, config: {} }
    ]);
    expect(details.join('\n')).toContain('id é obrigatório');
    expect(details.join('\n')).toContain('enabled deve ser booleano');
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
    expect(details.join('\n')).toContain("desconhecido 'notAField'");
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
    expect(joined).toContain("valores de 'urgency'");
    expect(joined).toContain("'actions' deve ser uma lista");
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
    expect(validateConfigDraft(null)).toEqual(['config deve ser um objeto']);
    expect(validateConfigDraft('str')).toEqual(['config deve ser um objeto']);
  });
});

describe('validateDraftFields (loop2 iterB inline rules)', () => {
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

  test('url requires a non-empty http(s) address', () => {
    expect(validateDraftFields({ ...valid, ollamaUrl: '' }).ollamaUrl).toContain('non-empty');
    expect(validateDraftFields({ ...valid, ollamaUrl: '127.0.0.1:11434' }).ollamaUrl).toContain('http:// or https://');
    expect(validateDraftFields({ ...valid, ollamaUrl: 'ftp://x' }).ollamaUrl).toContain('http:// or https://');
    expect(validateDraftFields({ ...valid, ollamaUrl: 'https://ollama.lan' }).ollamaUrl).toBeUndefined();
  });

  test('model, timeout, min_confidence and poll_interval follow the API rules', () => {
    expect(validateDraftFields({ ...valid, ollamaModel: '  ' }).ollamaModel).toContain('empty');
    expect(validateDraftFields({ ...valid, ollamaTimeout: '0' }).ollamaTimeout).toContain('> 0');
    expect(validateDraftFields({ ...valid, ollamaTimeout: 'abc' }).ollamaTimeout).toContain('> 0');
    expect(validateDraftFields({ ...valid, minConfidence: '1.2' }).minConfidence).toContain('between 0 and 1');
    expect(validateDraftFields({ ...valid, minConfidence: '-0.1' }).minConfidence).toContain('between 0 and 1');
    expect(validateDraftFields({ ...valid, pollInterval: 'abc' }).pollInterval).toContain('> 0');
    expect(validateDraftFields({ ...valid, pollInterval: '0' }).pollInterval).toContain('> 0');
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
    expect(fieldErrors.ollamaUrl).toContain('Ollama URL');
    expect(fieldErrors.ollamaModel).toContain('model');
    expect(fieldErrors.ollamaTimeout).toContain('> 0');
    expect(fieldErrors.minConfidence).toContain('between 0 and 1');
    expect(fieldErrors.pollInterval).toContain('> 0');
    expect(unmapped).toEqual([]);
  });

  test('keeps non-field details in the unmapped banner list', () => {
    const { fieldErrors, unmapped } = splitDetailsByField([
      "regra 'x': campo de condição desconhecido 'weird' (permitidos: app)",
      "'ollama.model' deve ser uma string não vazia"
    ]);
    expect(fieldErrors.ollamaModel).toBeDefined();
    expect(unmapped).toHaveLength(1);
    expect(unmapped[0]).toContain('desconhecido');
  });

  test('maps the bare 400 error text when details are absent', () => {
    const { fieldErrors } = splitDetailsByField(["config inválida: 'poll_interval' deve ser um número > 0"]);
    expect(fieldErrors.pollInterval).toBeDefined();
  });

  test('first field detail wins on duplicates', () => {
    const { fieldErrors } = splitDetailsByField([
      "'ollama.url' deve ser uma string não vazia",
      "'ollama.url' outro detalhe"
    ]);
    expect(fieldErrors.ollamaUrl).toContain('Ollama URL');
  });
});