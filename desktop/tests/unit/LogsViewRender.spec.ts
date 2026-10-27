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
import { LOGS_DENSITY_KEY } from '../../src/viewModels/logsViewModel';
import LogsView from '../../src/views/LogsView.vue';

const SCRIPT_LINE = '{"title": "<script>alert(1)</script>", "count": 42, "telegram": true}';
const STARTUP_LINE = '{"event": "startup", "mode": "daemon"}';

let pinia: Pinia;

async function mountLogs(raws: string[]): Promise<VueWrapper<any>> {
  pinia = createPinia();
  setActivePinia(pinia);
  const store = useBotStore();
  store.logLines = raws.map((raw, i) => ({ line_no: i + 1, raw, malformed: false }));
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