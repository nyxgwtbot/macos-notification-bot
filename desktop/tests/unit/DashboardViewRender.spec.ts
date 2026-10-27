/**
 * R6 rendered-DOM proof for the Status view (DashboardView):
 * - Space activates the expandable feed rows (ARIA button parity with Enter)
 * - feed growth announces through the visually-hidden polite region, driven
 *   by the events-length delta — and the list itself stays NON-live
 * - the feed container exposes aria-busy while feedLoading, and the designed
 *   center-spinner reload cue fires during the kind-switch reload
 * - the restart arm flip lives on a stable polite span ("Confirm restart?")
 */

import { flushPromises, mount, type VueWrapper } from '@vue/test-utils';
import { createPinia, setActivePinia, type Pinia } from 'pinia';
import { afterEach, describe, expect, test, vi } from 'vitest';

import { useBotStore } from '../../src/stores/bot';
import DashboardView from '../../src/views/DashboardView.vue';
import type { BotEvent } from '../../src/types/bot';

function notifEvent(recId: number): BotEvent {
  return {
    event: 'notification',
    timestamp: `2026-10-27T12:0${recId % 10}:00.000Z`,
    rec_id: recId,
    app: 'com.apple.mail',
    title: 'Fatura',
    classification: { category: 'finance', urgency: 'medium', summary: 'ok' }
  };
}

let pinia: Pinia;
let mounted: VueWrapper<any> | null = null;

async function mountStatus(events: BotEvent[] = []): Promise<VueWrapper<any>> {
  pinia = createPinia();
  setActivePinia(pinia);
  const store = useBotStore();
  for (const event of events) store.appendToFeed(event);
  // the 2s pollers would otherwise race a REAL bot API (logs spec precedent)
  vi.spyOn(store, 'pollFeed').mockResolvedValue(undefined);
  vi.spyOn(store, 'loadHealth').mockResolvedValue(undefined);
  vi.spyOn(store, 'pollDaemon').mockResolvedValue(undefined);
  const wrapper = mount(DashboardView, { global: { plugins: [pinia] } });
  mounted = wrapper;
  await flushPromises();
  return wrapper;
}

afterEach(() => {
  mounted?.unmount();
  mounted = null;
});

describe('DashboardView SR semantics + motion base (R6)', () => {
  test('Space activates the focused feed row, exactly like Enter', async () => {
    const wrapper = await mountStatus([notifEvent(1), notifEvent(2)]);
    const row = wrapper.find('.nb-event');
    expect(row.attributes('role')).toBe('button');
    expect(row.attributes('aria-expanded')).toBe('false');

    await row.trigger('keydown', { key: ' ' });
    expect(row.attributes('aria-expanded')).toBe('true');
    await row.trigger('keydown', { key: ' ' });
    expect(row.attributes('aria-expanded')).toBe('false');

    // Enter parity is preserved
    await row.trigger('keydown', { key: 'Enter' });
    expect(row.attributes('aria-expanded')).toBe('true');
  });

  test('feed growth announces via the sr-only polite region, driven by the length delta', async () => {
    const wrapper = await mountStatus([]);
    const region = wrapper.find('.nb-sr-only');
    expect(region.attributes('aria-live')).toBe('polite');
    expect(region.text()).toBe('');

    const store = useBotStore();
    store.appendToFeed(notifEvent(1));
    await flushPromises();
    await wrapper.vm.$nextTick();
    expect(wrapper.find('.nb-sr-only').text()).toBe('1 new event');

    // two more events in one flush -> one delta announcement, not two
    store.appendToFeed(notifEvent(2));
    store.appendToFeed(notifEvent(3));
    await flushPromises();
    await wrapper.vm.$nextTick();
    expect(wrapper.find('.nb-sr-only').text()).toBe('2 new events');

    // kind-switch reload shrinks the ring: stale count clears silently
    store.resetFeed();
    await flushPromises();
    await wrapper.vm.$nextTick();
    expect(wrapper.find('.nb-sr-only').text()).toBe('');
  });

  test('the feed list itself carries no live semantics (2s cadence stays quiet)', async () => {
    const wrapper = await mountStatus([notifEvent(1)]);
    expect(wrapper.find('.nb-feed').attributes('aria-live')).toBeUndefined();
  });

  test('the feed container binds aria-busy to feedLoading', async () => {
    const wrapper = await mountStatus([notifEvent(1)]);
    const feed = wrapper.find('.nb-feed');
    expect(feed.attributes('aria-busy')).toBe('false');

    const store = useBotStore();
    store.feedLoading = true;
    await wrapper.vm.$nextTick();
    expect(wrapper.find('.nb-feed').attributes('aria-busy')).toBe('true');

    store.feedLoading = false;
    await wrapper.vm.$nextTick();
    expect(wrapper.find('.nb-feed').attributes('aria-busy')).toBe('false');
  });

  test('the kind-switch reload fires the designed center-spinner cue while feedLoading', async () => {
    const wrapper = await mountStatus([notifEvent(1)]);
    const store = useBotStore();
    const pollFeedSpy = vi.spyOn(store, 'pollFeed');
    let resolveReload!: () => void;
    pollFeedSpy.mockImplementation(
      () => new Promise<void>((resolve) => { resolveReload = resolve; })
    );

    const reload = store.setFeedKind('notification');
    await wrapper.vm.$nextTick();
    expect(store.feedLoading).toBe(true);
    // ring is empty during the reload: the container yields to the spinner
    expect(wrapper.find('.nb-feed').exists()).toBe(false);
    expect(wrapper.find('.nb-center').exists()).toBe(true);

    resolveReload();
    await reload;
    await flushPromises();
    await wrapper.vm.$nextTick();
    expect(store.feedLoading).toBe(false);
    expect(wrapper.find('.nb-center').exists()).toBe(false);
  });

  test('the restart arm flip lives on a stable polite span', async () => {
    const wrapper = await mountStatus([]);
    const live = wrapper.find('.nb-card-footer span[aria-live="polite"]');
    expect(live.text()).toBe('Restart daemon');

    await wrapper.find('.nb-restart-btn').trigger('click');
    await wrapper.vm.$nextTick();
    expect(wrapper.find('.nb-card-footer span[aria-live="polite"]').text()).toBe('Confirm restart?');
  });
});