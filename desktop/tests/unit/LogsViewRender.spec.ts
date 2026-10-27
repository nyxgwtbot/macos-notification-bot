/**
 * R3 rendered-DOM proof for the Logs tail renderer (LogsView). The token
 * dimming builds interpolated text nodes wrapped by classed spans — the
 * escape-then-wrap order (Vue HTML-escapes every interpolation BEFORE the
 * spans wrap it, with no innerHTML path at all) is proven here at the DOM
 * level: a line containing `<script>` must render INERT. Also covers the
 * R3 search wiring (150ms debounce + "n of m lines" counter) and the
 * density chip persistence contract.
 */

import { flushPromises, mount, type VueWrapper } from '@vue/test-utils';
import { createPinia, setActivePinia, type Pinia } from 'pinia';
import { afterEach, describe, expect, test, vi } from 'vitest';

import { useBotStore } from '../../src/stores/bot';
import { LOGS_DENSITY_KEY, LOGS_FOLLOW_KEY, LOGS_KIND_FILTER_KEY } from '../../src/viewModels/logsViewModel';
import LogsView from '../../src/views/LogsView.vue';

const SCRIPT_LINE = '{"title": "<script>alert(1)</script>", "count": 42, "telegram": true}';
const STARTUP_LINE = '{"event": "startup", "mode": "daemon"}';
const NOTIF_LINE = '{"event": "notification", "telegram": "TELEGRAM_RETRY"}';
const MALFORMED_LINE = 'not-json at all';

let pinia: Pinia;

/** R7: entries are raw strings (malformed: false) or { raw, malformed }. */
async function mountLogs(entries: Array<string | { raw: string; malformed: boolean }>): Promise<VueWrapper<any>> {
  pinia = createPinia();
  setActivePinia(pinia);
  const store = useBotStore();
  store.logLines = entries.map((entry, i) => ({
    line_no: i + 1,
    raw: typeof entry === 'string' ? entry : entry.raw,
    malformed: typeof entry === 'string' ? false : entry.malformed
  }));
  // the 2s tail poller would otherwise race the spec against a REAL running
  // bot API and overwrite the preset tail — pin it to the preset
  vi.spyOn(store, 'pollLogs').mockResolvedValue(undefined);
  const wrapper = mount(LogsView, { global: { plugins: [pinia] } });
  await flushPromises();
  return wrapper;
}

describe('LogsView rendered log lines (R3)', () => {
  afterEach(() => {
    localStorage.clear();
  });

  test('a <script> line renders inert: escaped text only, plus dimmed tokens', async () => {
    const wrapper = await mountLogs([SCRIPT_LINE, STARTUP_LINE]);
    const raws = wrapper.findAll('.nb-raw');
    expect(raws.length).toBe(2);

    // no script markup anywhere in the rendered DOM
    expect(wrapper.element.querySelectorAll('script').length).toBe(0);
    // escape-then-wrap: hostile text reaches the DOM entity-escaped, not as markup
    expect(raws[0].element.innerHTML).toContain('&lt;script&gt;');
    // the tokenizer dropped nothing — raw text survives intact as text content
    expect(raws[0].element.textContent).toBe(SCRIPT_LINE);

    // token dimming still wraps keys and numeric/bool scalars
    const keys = raws[0].findAll('.nb-tok-key').map((s) => s.text());
    expect(keys).toContain('"title"');
    const nums = raws[0].findAll('.nb-tok-num').map((s) => s.text());
    expect(nums).toContain('42');
    expect(nums).toContain('true');

    wrapper.unmount();
  });

  test('search filters rows after the 150ms debounce and updates the "n of m lines" counter', async () => {
    const wrapper = await mountLogs([
      '{"event": "notification", "telegram": "failed"}',
      STARTUP_LINE,
      '{"event": "notification", "telegram": "ok"}'
    ]);
    expect(wrapper.findAll('.nb-line').length).toBe(3);
    expect(wrapper.find('.nb-log-count').text()).toBe('3 of 3 lines');

    await wrapper.find('input.nb-log-search').setValue('telegram');
    await flushPromises();
    await new Promise((resolve) => setTimeout(resolve, 200)); // > 150ms debounce
    await flushPromises();

    expect(wrapper.findAll('.nb-line').length).toBe(2);
    expect(wrapper.find('.nb-log-count').text()).toBe('2 of 3 lines');

    wrapper.unmount();
  });

  test('density chip persists to localStorage and drives the scroller data-density', async () => {
    const wrapper = await mountLogs([STARTUP_LINE]);
    const scroller = wrapper.find('.nb-log-scroller');
    // comfortable is the default for a fresh store
    expect(scroller.attributes('data-density')).toBe('comfortable');
    const chips = wrapper.findAll('.nb-density .nb-filter-chip');
    expect(chips.map((c) => c.text())).toEqual(['Compact', 'Comfortable']);

    await chips[0].trigger('click');
    expect(scroller.attributes('data-density')).toBe('compact');
    expect(localStorage.getItem(LOGS_DENSITY_KEY)).toBe('compact');

    await chips[1].trigger('click');
    expect(scroller.attributes('data-density')).toBe('comfortable');
    expect(localStorage.getItem(LOGS_DENSITY_KEY)).toBe('comfortable');

    wrapper.unmount();
  });
});

/* ========================================
   R7 rendered-DOM proofs: log-landmark semantics + cap line + line-number
   gutter, bad-row title, query-highlight (escape-then-wrap intact at the DOM
   level), follow/kind-filter persistence, and the +N badge SR announce.
   ======================================== */
describe('LogsView R7 semantics, persistence, highlight', () => {
  afterEach(() => {
    localStorage.clear();
  });

  test('the tail renders as a labeled log landmark: cap line, gutters, bad-row title', async () => {
    const wrapper = await mountLogs([NOTIF_LINE, { raw: MALFORMED_LINE, malformed: true }]);
    const scroller = wrapper.find('.nb-log-scroller');
    expect(scroller.attributes('role')).toBe('log');
    expect(scroller.attributes('aria-label')).toBe('Raw log tail');
    expect(scroller.attributes('tabindex')).toBe('0');
    expect(wrapper.find('.nb-cap').text()).toBe('— start of loaded tail —');

    const rows = wrapper.findAll('.nb-line');
    expect(rows).toHaveLength(2);
    expect(rows[0].find('.nb-line-no').text()).toBe('1');
    expect(rows[1].find('.nb-line-no').text()).toBe('2');
    expect(rows[1].classes()).toContain('nb-line-bad');
    expect(rows[1].attributes('title')).toBe('Malformed JSON line');
    expect(rows[0].attributes('title')).toBeUndefined();

    wrapper.unmount();
  });

  test('search matches render as nb-tok-hi spans, with token dimming around them', async () => {
    const wrapper = await mountLogs([NOTIF_LINE, STARTUP_LINE]);
    await wrapper.find('input.nb-log-search').setValue('telegram');
    await flushPromises();
    await new Promise((resolve) => setTimeout(resolve, 200)); // > 150ms debounce
    await flushPromises();

    const his = wrapper.findAll('.nb-tok-hi').map((s) => s.text());
    // the key splits around the match: quote marks stay plain, the matched
    // substring re-classes; the value match keeps its full case
    expect(his).toEqual(['telegram', 'TELEGRAM']);
    // dimmed keys still wrap the non-matched pieces
    expect(wrapper.findAll('.nb-tok-key').map((s) => s.text())).toContain('"event"');

    wrapper.unmount();
  });

  test('a <script> query highlight stays inert — escaped text inside the highlight span', async () => {
    const wrapper = await mountLogs([SCRIPT_LINE]);
    await wrapper.find('input.nb-log-search').setValue('"<script');
    await flushPromises();
    await new Promise((resolve) => setTimeout(resolve, 200));
    await flushPromises();

    const his = wrapper.findAll('.nb-tok-hi');
    expect(his).toHaveLength(1);
    expect(his[0].text()).toBe('"<script');
    // the highlight span itself carries ESCAPED text — no markup inside
    expect(his[0].element.innerHTML).toContain('&lt;script');
    expect(his[0].element.innerHTML).not.toContain('<script');
    // no script markup anywhere in the rendered DOM, and the raw line still
    // reconstructs exactly through the segmented render path
    expect(wrapper.element.querySelectorAll('script').length).toBe(0);
    expect(wrapper.findAll('.nb-raw')[0].text()).toBe(SCRIPT_LINE);

    wrapper.unmount();
  });

  test('kind filter persists per chip click; a fresh mount re-applies it', async () => {
    const wrapper = await mountLogs([NOTIF_LINE, STARTUP_LINE]);
    const chips = wrapper.findAll('.nb-filter-chips .nb-filter-chip');
    await chips[1].trigger('click'); // Events
    expect(localStorage.getItem(LOGS_KIND_FILTER_KEY)).toBe('notification');
    wrapper.unmount();

    const restored = await mountLogs([NOTIF_LINE, STARTUP_LINE]);
    expect(restored.findAll('.nb-line').length).toBe(1);
    const restoredChips = restored.findAll('.nb-filter-chips .nb-filter-chip');
    expect(restoredChips[1].attributes('data-active')).toBe('true');
    restored.unmount();
  });

  test('a tampered stored kind filter degrades to All', async () => {
    localStorage.setItem(LOGS_KIND_FILTER_KEY, 'startup!!!');
    const wrapper = await mountLogs([NOTIF_LINE, STARTUP_LINE]);
    expect(wrapper.findAll('.nb-line').length).toBe(2);
    const chips = wrapper.findAll('.nb-filter-chips .nb-filter-chip');
    expect(chips[0].attributes('data-active')).toBe('true');
    wrapper.unmount();
  });

  test('pausing writes the follow key; a stored paused state restores on mount', async () => {
    const wrapper = await mountLogs([NOTIF_LINE, STARTUP_LINE]);
    const chip = wrapper.find('.nb-follow-chip');
    expect(chip.attributes('aria-pressed')).toBe('true');
    await chip.trigger('click'); // pause
    expect(localStorage.getItem(LOGS_FOLLOW_KEY)).toBe('false');
    wrapper.unmount();

    localStorage.setItem(LOGS_FOLLOW_KEY, 'false');
    const restored = await mountLogs([NOTIF_LINE, STARTUP_LINE]);
    const restoredChip = restored.find('.nb-follow-chip');
    expect(restoredChip.attributes('aria-pressed')).toBe('false');
    expect(restoredChip.text()).toBe('Paused — follow?');

    await restoredChip.trigger('click'); // re-enable writes back 'true'
    expect(localStorage.getItem(LOGS_FOLLOW_KEY)).toBe('true');
    restored.unmount();
  });

  test('paused growth shows the +N badge and announces "N new log lines" politely', async () => {
    const wrapper = await mountLogs([NOTIF_LINE, STARTUP_LINE]);
    const store = useBotStore();
    await wrapper.find('.nb-follow-chip').trigger('click'); // pause
    store.logLines = [
      ...store.logLines,
      { line_no: 3, raw: '{"event": "notification", "state": "SENT"}', malformed: false }
    ];
    await flushPromises();
    await flushPromises();

    const newBadge = wrapper
      .findAll('.nb-log-header-end ion-badge')
      .find((b) => b.text().includes('new'));
    expect(newBadge?.text()).toBe('+1 new');
    expect(wrapper.find('.nb-sr-only').text()).toBe('1 new log line');

    wrapper.unmount();
  });
});