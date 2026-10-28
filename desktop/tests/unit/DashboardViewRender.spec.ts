/**
 * R6 rendered-DOM proof for the Status view (DashboardView):
 * - Space activates the expandable feed rows (ARIA button parity with Enter)
 * - feed growth announces through the visually-hidden polite region, driven
 *   by the events-length delta — and the list itself stays NON-live
 * - the feed container exposes aria-busy while feedLoading, and the designed
 *   center-spinner reload cue fires during the kind-switch reload
 * - the restart arm flip lives on a stable polite span ("Confirm restart?")
 * R8:
 * - the header badge is the shared "Live" badge (cadence in the tooltip)
 * - polling sleeps while hidden; resume fires the catch-up tick that
 *   recomputes the age labels (timestamps stay honest post-resume)
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

/* ========================================
   R8 shell semantics: the shared Live badge wording + the polling visibility
   pause, proven at the DOM level.
   ======================================== */
describe('DashboardView R8 — Live badge + visibility pause', () => {
  function setHidden(hidden: boolean): void {
    Object.defineProperty(document, 'hidden', { configurable: true, get: () => hidden });
  }
  function fireVisibilityChange(): void {
    document.dispatchEvent(new Event('visibilitychange'));
  }

  afterEach(() => {
    setHidden(false); // restore the jsdom default
    vi.useRealTimers();
  });

  test('the header badge reads "Live" with the poll cadence in its tooltip; errors flip the tint', async () => {
    const wrapper = await mountStatus([notifEvent(1)]);
    const badge = wrapper.find('.nb-head-end .nb-live-badge');
    expect(badge.exists()).toBe(true);
    expect(badge.text()).toBe('Live');
    expect(badge.attributes('title')).toBe('Refreshes every 2 s');
    expect(badge.attributes('data-state')).toBe('live');

    const store = useBotStore();
    store.feedError = 'offline boom';
    await wrapper.vm.$nextTick();
    expect(wrapper.find('.nb-head-end .nb-live-badge').attributes('data-state')).toBe('error');
  });

  test('polling sleeps while hidden; the resume catch-up tick recomputes the age labels', async () => {
    // fake timers freeze BOTH the cadence and Date.now (setSystemTime)
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-10-27T12:01:59Z'));

    const settle = async (wrapper: VueWrapper<any>): Promise<void> => {
      await vi.advanceTimersByTimeAsync(0);
      await wrapper.vm.$nextTick();
      await vi.advanceTimersByTimeAsync(0);
      await wrapper.vm.$nextTick();
    };

    pinia = createPinia();
    setActivePinia(pinia);
    const store = useBotStore();
    const event = { ...notifEvent(1), timestamp: '2026-10-27T12:00:00.000Z' };
    store.appendToFeed(event);
    vi.spyOn(store, 'pollFeed').mockResolvedValue(undefined);
    vi.spyOn(store, 'loadHealth').mockResolvedValue(undefined);
    vi.spyOn(store, 'pollDaemon').mockResolvedValue(undefined);
    const wrapper = mount(DashboardView, { global: { plugins: [pinia] } });
    await settle(wrapper);
    mounted = wrapper;

    const feedCalls = () => vi.mocked(store.pollFeed).mock.calls.length;
    const ageText = () => wrapper.find('.nb-when-rel').text();
    // fresh mount: the immediate first run stamped the age against the frozen clock
    expect(feedCalls()).toBe(1);
    expect(ageText()).toBe('1 min ago');

    // hidden: the cadence sleeps — the clock advances but nothing recomputes
    setHidden(true);
    fireVisibilityChange();
    await vi.advanceTimersByTimeAsync(5000);
    await wrapper.vm.$nextTick();
    expect(feedCalls()).toBe(1); // no ticking in the background
    expect(ageText()).toBe('1 min ago'); // display honestly frozen

    // visible: ONE catch-up refresh RIGHT AWAY — the age recomputes from the
    // nowMs bump inside the poll task (timestamps honest again)
    setHidden(false);
    fireVisibilityChange();
    await settle(wrapper);
    expect(feedCalls()).toBe(2);
    expect(ageText()).toBe('2 min ago');

    // ...and the interval resumed behind the catch-up run
    await vi.advanceTimersByTimeAsync(2000);
    await wrapper.vm.$nextTick();
    expect(feedCalls()).toBe(3);
    expect(ageText()).toBe('2 min ago'); // 127s -> floor = 2 min
    wrapper.unmount();
    mounted = null;
  });
});

describe('DashboardView feed digest + caption + machine mono (S3, S4, S5)', () => {
  function startupEvent(minute: number): BotEvent {
    return {
      event: 'startup',
      timestamp: `2026-10-27T12:0${minute}:00.000Z`,
      mode: 'daemon',
      watermark: minute
    };
  }

  test('3 startup rows collapse into a single digest row with row DNA; expand reveals inline rows with expanded caption', async () => {
    const s1 = startupEvent(1);
    const s2 = startupEvent(2);
    const s3 = startupEvent(3);
    const wrapper = await mountStatus([s1, s2, s3]);

    // Top-level feed should have 1 digest item, not 3 raw rows
    const digests = wrapper.findAll('.nb-feed > .nb-event-digest');
    expect(digests).toHaveLength(1);

    const digest = digests[0];
    expect(digest.attributes('role')).toBe('button');
    expect(digest.attributes('aria-expanded')).toBe('false');
    expect(digest.find('.nb-digest-label strong').text()).toBe('3');
    expect(digest.find('.nb-digest-label').text()).toContain('3 daemon starts');

    // No collapsed kind caption (.nb-kind-caption) anywhere
    expect(wrapper.find('.nb-kind-caption').exists()).toBe(false);

    // Expand via click
    await digest.trigger('click');
    expect(digest.attributes('aria-expanded')).toBe('true');

    // Inside the digest, 3 rows are rendered inline
    const inlineRows = digest.findAll('.nb-digest-rows .nb-event-startup');
    expect(inlineRows).toHaveLength(3);

    // Expand first inline startup row to check expandedRowCaption
    await inlineRows[0].trigger('click');
    const caption = inlineRows[0].find('.nb-event-raw-source');
    expect(caption.exists()).toBe(true);
    expect(caption.text()).toBe('startup · bot');
  });

  test('digest and every child expand independently with click, Enter and Space', async () => {
    const wrapper = await mountStatus([startupEvent(1), startupEvent(2), startupEvent(3)]);
    const digest = wrapper.get('.nb-event-digest');
    await digest.trigger('keydown', { key: 'Enter' });
    const rows = digest.findAll('.nb-digest-rows .nb-event-startup');
    expect(rows).toHaveLength(3);
    for (const row of rows) {
      expect(row.attributes('aria-expanded')).toBe('false');
      await row.trigger('click');
      expect(row.attributes('aria-expanded')).toBe('true');
      expect(digest.attributes('aria-expanded')).toBe('true');
      await row.get('.nb-raw-copy').trigger('keydown', { key: 'Enter' });
      expect(row.attributes('aria-expanded')).toBe('true');
      expect(digest.attributes('aria-expanded')).toBe('true');
      await row.trigger('keydown', { key: ' ' });
      expect(row.attributes('aria-expanded')).toBe('false');
      expect(digest.attributes('aria-expanded')).toBe('true');
      await row.trigger('keydown', { key: 'Enter' });
      expect(row.find('.nb-event-raw').exists()).toBe(true);
      expect(digest.attributes('aria-expanded')).toBe('true');
    }
    useBotStore().appendToFeed(startupEvent(4));
    await wrapper.vm.$nextTick();
    expect(wrapper.findAll('.nb-event-digest')).toHaveLength(1);
    expect(digest.attributes('aria-expanded')).toBe('true');
    expect(digest.findAll('.nb-digest-rows .nb-event-startup')).toHaveLength(4);
    await digest.trigger('keydown', { key: ' ' });
    expect(digest.attributes('aria-expanded')).toBe('false');
  });

  test('machine values mono (S3): Ollama and Model rows carry nb-kv-tech and title attrs', async () => {
    const wrapper = await mountStatus([]);
    const store = useBotStore();
    store.health = {
      status: 'ok',
      bot_version: '1.0.0',
      api_version: '1.0.0',
      watermark: { last_rec_id: 10, updated_at: '2026-10-27T12:00:00Z' },
      log: { bytes: 1024 },
      finance: { enabled: false, db_exists: false },
      ollama: { url: 'http://127.0.0.1:11434', model: 'llama3:8b' }
    } as any;
    await wrapper.vm.$nextTick();

    const techRows = wrapper.findAll('.nb-kv-tech');
    expect(techRows.length).toBeGreaterThanOrEqual(2);

    const ollamaStrong = techRows[0].find('strong');
    expect(ollamaStrong.attributes('title')).toBe('http://127.0.0.1:11434');
    expect(ollamaStrong.text()).toBe('http://127.0.0.1:11434');

    const modelStrong = techRows[1].find('strong');
    expect(modelStrong.attributes('title')).toBe('llama3:8b');
    expect(modelStrong.text()).toBe('llama3:8b');
  });
});
