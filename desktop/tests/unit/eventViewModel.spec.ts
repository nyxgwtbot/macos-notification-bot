import { describe, expect, test } from 'vitest';

import {
  confidenceToPct,
  eventKey,
  eventToRow,
  isErrorStatus
} from '../../src/viewModels/eventViewModel';
import type { BotEvent } from '../../src/types/bot';

function notifEvent(overrides: Partial<BotEvent> = {}): BotEvent {
  return {
    event: 'notification',
    timestamp: '2026-10-26T12:00:00.000-03:00',
    rec_id: 42,
    app: 'com.apple.mail',
    uuid: 'abc123',
    title: 'Fatura',
    subtitle: '',
    body: 'Compra aprovada de R$ 1.234,56',
    classification: { category: 'finance', urgency: 'medium', summary: 'Compra de R$ 1.234,56' },
    layer: 'llm_router',
    rule_matched: 'none',
    confidence: 0.87,
    router_payload: { action_id: 'finance' },
    actions: ['finance', 'log'],
    action_results: { log: 'ok' },
    ...overrides
  };
}

describe('eventToRow', () => {
  test('maps a full notification event into a row', () => {
    const row = eventToRow(notifEvent());
    expect(row.kind).toBe('notification');
    expect(row.recId).toBe(42);
    expect(row.app).toBe('com.apple.mail');
    expect(row.title).toBe('Fatura');
    expect(row.category).toBe('finance');
    expect(row.urgency).toBe('medium');
    expect(row.summary).toBe('Compra de R$ 1.234,56');
    expect(row.layer).toBe('llm_router');
    expect(row.layerLabel).toBe('Router LLM');
    expect(row.ruleMatched).toBe(''); // 'none' suppressed
    expect(row.actions).toEqual(['finance', 'log']);
    expect(row.confidencePct).toBe('87%');
  });

  test('classification defaults follow the classifier fallback (other/low/title-summary)', () => {
    const row = eventToRow(notifEvent({ classification: undefined as never }));
    expect(row.category).toBe('other');
    expect(row.urgency).toBe('low');
    expect(row.summary).toBe('Fatura');
  });

  test('missing app falls back and startup events show daemon mode', () => {
    const row = eventToRow(notifEvent({ app: null as never }));
    expect(row.app).toBe('—');

    const startup = eventToRow({
      event: 'startup',
      timestamp: '2026-10-26T12:00:00Z',
      mode: 'daemon',
      watermark: 12
    });
    expect(startup.kind).toBe('startup');
    expect(startup.summary).toBe("Bot iniciado no modo 'daemon'");
    expect(startup.layer).toBe('');
    expect(startup.confidencePct).toBe(null);
  });

  test('layer labels cover all Router v2 layers', () => {
    expect(eventToRow(notifEvent({ layer: 'rule' as never })).layerLabel).toBe('Regra');
    expect(eventToRow(notifEvent({ layer: 'fallback_in_fallback' as never })).layerLabel).toBe('Fallback');
  });

  test('ruleMatched keeps real rule names only', () => {
    expect(eventToRow(notifEvent({ rule_matched: 'high_urgency' })).ruleMatched).toBe('high_urgency');
    expect(eventToRow(notifEvent({ rule_matched: 'none' })).ruleMatched).toBe('');
    expect(eventToRow(notifEvent({ rule_matched: undefined as never })).ruleMatched).toBe('');
  });

  test('telegram chip prefers event.telegram, falls back to action_results', () => {
    expect(eventToRow(notifEvent({ telegram: 'ok' })).telegramStatus).toBe('ok');
    const row = eventToRow(notifEvent({ telegram: undefined as never, action_results: { log: 'ok', telegram: 'skipped' } }));
    expect(row.telegramStatus).toBe('skipped');
  });

  test('finance status chip composes status:method:stored(:reason)', () => {
    expect(
      eventToRow(
        notifEvent({
          finance: { status: 'ok', method: 'regex', stored: 'inserted', rec_id: 42 }
        })
      ).financeStatus
    ).toBe('ok:regex:inserted');
    expect(
      eventToRow(
        notifEvent({
          finance: { status: 'skipped', reason: 'no_amount' }
        })
      ).financeStatus
    ).toBe('skipped:no_amount');
  });
});

describe('confidenceToPct', () => {
  test('maps 0..1 fractions to percentages and rejects outliers', () => {
    expect(confidenceToPct(0.87)).toBe('87%');
    expect(confidenceToPct(1)).toBe('100%');
    expect(confidenceToPct(0)).toBe('0%');
    expect(confidenceToPct(1.5)).toBe(null);
    expect(confidenceToPct(-0.2)).toBe(null);
    expect(confidenceToPct(null)).toBe(null);
    expect(confidenceToPct(undefined)).toBe(null);
    expect(confidenceToPct(Number.NaN)).toBe(null);
  });
});

describe('eventKey / isErrorStatus', () => {
  test('keys dedupe notifications by rec_id and startup by timestamp', () => {
    expect(eventKey(notifEvent())).toBe('notification:42');
    expect(eventKey(notifEvent())).toBe(eventKey(notifEvent()));
    const startup = eventToRow({
      event: 'startup',
      timestamp: '2026-10-26T12:00:00Z'
    });
    expect(startup.key).toBe('startup:2026-10-26T12:00:00Z');
  });

  test('error chips are detected only for error-prefixed statuses', () => {
    expect(isErrorStatus('error: timeout')).toBe(true);
    expect(isErrorStatus('ok')).toBe(false);
    expect(isErrorStatus(null)).toBe(false);
  });
});