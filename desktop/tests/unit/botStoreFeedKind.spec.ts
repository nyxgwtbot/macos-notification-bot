/**
 * Feed kind filter store flow (loop2 iterB): `kind=` must travel with every
 * poll, and changing the filter performs a FULL reload (ring + since cursor
 * reset) followed by one immediate poll.
 */

import { beforeEach, describe, expect, test, vi } from 'vitest';
import { createPinia, setActivePinia } from 'pinia';

const getEvents = vi.fn();

vi.mock('@/services/notifyClient', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/services/notifyClient')>();
  return {
    ...actual,
    notifyClient: {
      ...actual.notifyClient,
      getEvents: (options: unknown) => getEvents(options)
    }
  };
});

import { useBotStore } from '@/stores/bot';
import type { BotEvent } from '@/types/bot';

function notifEvent(recId: number): BotEvent {
  return {
    event: 'notification',
    timestamp: `2026-10-27T12:0${recId}:00.000Z`,
    rec_id: recId,
    app: 'com.apple.mail',
    title: 'Fatura',
    classification: { category: 'finance', urgency: 'medium', summary: 'ok' }
  };
}

describe('feed kind filter (stores/bot)', () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    getEvents.mockReset();
    getEvents.mockResolvedValue({ events: [], latest_rec_id: null, count: 0 });
  });

  test('default is All (no kind param) and the cursor behavior is unchanged', async () => {
    const store = useBotStore();
    getEvents.mockResolvedValue({ events: [notifEvent(7)], latest_rec_id: 7, count: 1 });
    await store.pollFeed();
    expect(getEvents).toHaveBeenLastCalledWith({ limit: 100, since: undefined });
    expect(store.events).toHaveLength(1);
    expect(store.sinceCursor).toBe(7);
  });

  test('setFeedKind re-queries with kind= and reloads from scratch', async () => {
    const store = useBotStore();
    getEvents.mockResolvedValue({ events: [notifEvent(1)], latest_rec_id: 1, count: 1 });
    await store.pollFeed();

    await store.setFeedKind('notification');
    expect(store.feedKind).toBe('notification');
    // full reload: events + cursor cleared by resetFeed before the immediate poll
    expect(getEvents).toHaveBeenLastCalledWith({ limit: 100, since: undefined, kind: 'notification' });

    await store.setFeedKind('startup');
    expect(getEvents).toHaveBeenLastCalledWith({ limit: 100, since: undefined, kind: 'startup' });
  });

  test('polling keeps the filter applied across the 2s loop', async () => {
    const store = useBotStore();
    await store.setFeedKind('notification'); // 1st call: the immediate reload
    await store.pollFeed();
    await store.pollFeed();
    expect(getEvents).toHaveBeenCalledTimes(3);
    expect(getEvents).toHaveBeenLastCalledWith({ limit: 100, kind: 'notification' });
  });

  test('Events→All drops the kind param and does a full reload', async () => {
    const store = useBotStore();
    await store.setFeedKind('notification');
    getEvents.mockResolvedValue({ events: [notifEvent(9)], latest_rec_id: 9, count: 1 });
    await store.pollFeed();
    await store.setFeedKind('all');
    expect(getEvents).toHaveBeenLastCalledWith({ limit: 100, since: undefined });
  });

  test('R6: feedLoading tracks only the kind-switch reload, not the 2s polls', async () => {
    const store = useBotStore();
    await store.pollFeed();
    expect(store.feedLoading).toBe(false); // incremental poll never flips it

    // hold the reload poll open and observe aria-busy's source flag
    let resolveReload!: (value: { events: BotEvent[]; latest_rec_id: number; count: number }) => void;
    getEvents.mockImplementation(
      () => new Promise((resolve) => { resolveReload = resolve; })
    );
    const reload = store.setFeedKind('notification');
    expect(store.feedLoading).toBe(true);

    resolveReload({ events: [notifEvent(2)], latest_rec_id: 2, count: 1 });
    await reload;
    expect(store.feedLoading).toBe(false);
    expect(store.sinceCursor).toBe(2);
  });

  test('R6: a failing reload still clears feedLoading (finally, no stuck aria-busy)', async () => {
    const store = useBotStore();
    getEvents.mockRejectedValue(new Error('down'));
    await store.setFeedKind('notification');
    expect(store.feedLoading).toBe(false);
    expect(store.feedError).toBeTruthy();
  });
});