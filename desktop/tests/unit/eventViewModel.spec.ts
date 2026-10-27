import { describe, expect, test } from 'vitest';

import {
  confidenceToPct,
  eventKey,
  eventKindIdentity,
  eventToRow,
  expandedRowCaption,
  feedIsAllStartup,
  isErrorStatus,
  payloadPreview,
  relativeTimeLabel,
  EVENT_KIND_OPTIONS
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

describe('payloadPreview', () => {
  test('pretty-prints the raw event payload under the 2K cap', () => {
    const event = notifEvent();
    const preview = payloadPreview(event);
    expect(preview.truncatedChars).toBe(0);
    expect(preview.text).toContain('"event": "notification"');
    expect(preview.text).toContain('"rec_id": 42');
    expect(preview.text.startsWith('{')).toBe(true);
  });

  test('trims the tail past 2048 chars and reports the overflow', () => {
    const event = notifEvent({ body: 'x'.repeat(4000) });
    const preview = payloadPreview(event);
    expect(preview.text.length).toBe(2048);
    expect(preview.truncatedChars).toBeGreaterThan(0);
  });

  test('never throws on a non-serializable payload', () => {
    const cyclic: Record<string, unknown> = {};
    cyclic.self = cyclic;
    const preview = payloadPreview(cyclic as unknown as BotEvent);
    expect(typeof preview.text).toBe('string');
  });
});

describe('relativeTimeLabel', () => {
  // Fixed render clock; timestamps are built as offsets from it.
  const NOW = Date.parse('2026-10-26T12:00:00.000Z');

  test('labels seconds as now', () => {
    expect(relativeTimeLabel(new Date(NOW).toISOString(), NOW)).toBe('now');
    expect(relativeTimeLabel(new Date(NOW - 30_000).toISOString(), NOW)).toBe('now');
    expect(relativeTimeLabel(new Date(NOW - 59_000).toISOString(), NOW)).toBe('now');
  });

  test('labels minutes / hours / days with the exact boundaries', () => {
    expect(relativeTimeLabel(new Date(NOW - 60_000).toISOString(), NOW)).toBe('1 min ago');
    expect(relativeTimeLabel(new Date(NOW - 3 * 60_000).toISOString(), NOW)).toBe('3 min ago');
    expect(relativeTimeLabel(new Date(NOW - 59 * 60_000).toISOString(), NOW)).toBe('59 min ago');
    expect(relativeTimeLabel(new Date(NOW - 60 * 60_000).toISOString(), NOW)).toBe('1 h ago');
    expect(relativeTimeLabel(new Date(NOW - 3 * 60 * 60_000).toISOString(), NOW)).toBe('3 h ago');
    expect(relativeTimeLabel(new Date(NOW - 23 * 60 * 60_000).toISOString(), NOW)).toBe('23 h ago');
    expect(relativeTimeLabel(new Date(NOW - 24 * 60 * 60_000).toISOString(), NOW)).toBe('1 d ago');
    expect(relativeTimeLabel(new Date(NOW - 2.5 * 24 * 60 * 60_000).toISOString(), NOW)).toBe('2 d ago');
  });

  test('future timestamps and clock skew read as now', () => {
    expect(relativeTimeLabel(new Date(NOW + 5_000).toISOString(), NOW)).toBe('now');
    expect(relativeTimeLabel(new Date(NOW + 10 * 60_000).toISOString(), NOW)).toBe('now');
  });

  test('empty, malformed or non-finite inputs degrade to empty string', () => {
    expect(relativeTimeLabel('', NOW)).toBe('');
    expect(relativeTimeLabel('not-a-date', NOW)).toBe('');
    expect(relativeTimeLabel(new Date(NOW).toISOString(), Number.NaN)).toBe('');
  });
});

describe('eventKindIdentity', () => {
  test('startup rows keep the startup identity', () => {
    const startup = eventToRow({ event: 'startup', timestamp: '2026-10-26T12:00:00Z', mode: 'daemon' });
    expect(eventKindIdentity(startup)).toBe('startup');
  });

  test('notification rows: finance identity only when the finance layer acted', () => {
    const plain = eventToRow(notifEvent());
    expect(eventKindIdentity(plain)).toBe('notification');

    const financeOk = eventToRow(notifEvent({ finance: { status: 'ok', method: 'regex', stored: 'inserted' } }));
    expect(eventKindIdentity(financeOk)).toBe('finance');

    // even a skipped finance pass is a finance row — the badge says so too
    const financeSkipped = eventToRow(notifEvent({ finance: { status: 'skipped', reason: 'no_amount' } }));
    expect(eventKindIdentity(financeSkipped)).toBe('finance');
  });
});

describe('EVENT_KIND_OPTIONS / raw passthrough', () => {
  test('kinds mirror the /api/events kind= values (all | notification | startup)', () => {
    expect(EVENT_KIND_OPTIONS.map((option) => option.value)).toEqual(['all', 'notification', 'startup']);
    expect(EVENT_KIND_OPTIONS.map((option) => option.label)).toEqual(['All', 'Events', 'Startup']);
  });

  test('eventToRow keeps the untouched raw event for the expandable payload', () => {
    const event = notifEvent({ confidence: 0.3, body: 'full body' });
    const row = eventToRow(event);
    expect(row.raw).toBe(event); // identity, not a copy
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

describe('feedIsAllStartup', () => {
  const startupRow = eventToRow({ event: 'startup', timestamp: '2026-10-26T12:00:00Z' });

  test('true when the feed has rows but zero notification-kind rows', () => {
    expect(feedIsAllStartup([startupRow])).toBe(true);
    expect(feedIsAllStartup([startupRow, { ...startupRow, key: 'startup:other' }])).toBe(true);
  });

  test('false the moment one notification-kind row appears (any position)', () => {
    const notificationRow = eventToRow(notifEvent());
    expect(feedIsAllStartup([notificationRow])).toBe(false);
    expect(feedIsAllStartup([startupRow, notificationRow])).toBe(false);
    expect(feedIsAllStartup([notificationRow, startupRow])).toBe(false);
  });

  test('an empty feed is not "all-startup" — it renders the plain empty state', () => {
    expect(feedIsAllStartup([])).toBe(false);
  });

  test('finance-acted rows keep kind notification, so they break the cue', () => {
    const financeRow = eventToRow(notifEvent({ finance: { status: 'ok', method: 'regex' } }));
    expect(financeRow.kind).toBe('notification');
    expect(feedIsAllStartup([startupRow, financeRow])).toBe(false);
  });
});

describe('expandedRowCaption', () => {
  test('notification rows caption as "app · Title" (the dropped bold label)', () => {
    expect(expandedRowCaption(eventToRow(notifEvent()))).toBe('com.apple.mail · Fatura');
  });

  test('startup rows keep the bot source tag (no title to append)', () => {
    const startup = eventToRow({ event: 'startup', timestamp: '2026-10-26T12:00:00Z' });
    expect(expandedRowCaption(startup)).toBe('bot');
  });

  test('unknown app ("—") degrades to the title alone; empty title drops out', () => {
    expect(expandedRowCaption(eventToRow(notifEvent({ app: null as never })))).toBe('Fatura');
    expect(expandedRowCaption(eventToRow(notifEvent({ title: null as never })))).toBe('com.apple.mail');
    // nothing meaningful at all -> the caption slot is skipped by the caller
    expect(expandedRowCaption(eventToRow(notifEvent({ app: null as never, title: null as never })))).toBe('');
  });
});
