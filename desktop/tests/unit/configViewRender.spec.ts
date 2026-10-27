/**
 * R8 rendered-DOM proof for the Settings banner flow (ConfigView):
 * - the save-blocking validation banner renders INSIDE the Bot & Ollama
 *   card, directly above the save row (the old page-bottom placement is
 *   gone) and a failed save scrolls it into view — 'nearest' + a behavior
 *   that degrades to 'auto' under prefers-reduced-motion (utils/motion)
 * - the Actions rows join the boxed+error treatment: an empty command on a
 *   type:'command' action blocks the save with the standard inline pair
 * - the Connectors error banner carries a Retry that re-raises loadConnectors
 */

import { flushPromises, mount, type VueWrapper } from '@vue/test-utils';
import { createPinia, setActivePinia, type Pinia } from 'pinia';
import {
  afterAll,
  afterEach,
  beforeAll,
  describe,
  expect,
  test,
  vi,
  type Mock
} from 'vitest';

import { useBotStore } from '../../src/stores/bot';
import { notifyClient } from '../../src/services/notifyClient';
import { ACTION_COMMAND_REQUIRED_TEXT } from '../../src/services/configValidation';
import ConfigView from '../../src/views/ConfigView.vue';
import type { BotConfig } from '../../src/types/bot';

function baseConfig(overrides: Partial<BotConfig> = {}): BotConfig {
  return {
    poll_interval: 30,
    state_file: '.bot_state.json',
    log_file: 'logs/events.jsonl',
    ollama: { url: 'http://127.0.0.1:11434/api/chat', model: 'glm-5.3-flash:cloud', timeout: 30 },
    telegram: { token: '', chat_id: '' },
    router: { min_confidence: 0.5, timeout: 15 },
    actions: [
      { id: 'clipboard-copy', match_hint: 'copy', type: 'builtin', enabled: true, config: {} },
      { id: 'open-file', match_hint: 'open', type: 'command', enabled: true, config: { command: 'open {path}' } }
    ],
    rules: [],
    finance: {
      enabled: false,
      ledger_db: 'finance.db',
      bank_apps: [],
      telegram_summary: false,
      categories: ['bank']
    },
    ...overrides
  };
}

function connectorsResponse() {
  return {
    connectors: {
      reminders: { status: 'granted', granted: true, last_probe: null, hint: 'Reminders.app' },
      notes: { status: 'needs_consent', granted: false, last_probe: null, hint: 'Notes.app' },
      system: { status: 'unknown', granted: false, last_probe: null, hint: '' }
    }
  };
}

let pinia: Pinia | null = null;
let mounted: VueWrapper<any> | null = null;
/** (target, options) pairs recorded from Element.prototype.scrollIntoView
 * (jsdom has no scrollIntoView, and this vitest's mock fn does not track
 * `this` — a hand-rolled shim records the receiver element honestly). */
let scrollCalls: Array<{ target: Element; options?: ScrollIntoViewOptions }>;

function lastScrollCall(): { target: Element; options?: ScrollIntoViewOptions } {
  expect(scrollCalls.length).toBeGreaterThan(0);
  return scrollCalls[scrollCalls.length - 1];
}

async function mountConfig(
  config: BotConfig,
  options: { connectorsReject?: string } = {}
): Promise<{ wrapper: VueWrapper<any>; saveConfigSpy: Mock; connectorsSpy: Mock }> {
  pinia = createPinia();
  setActivePinia(pinia);
  const store = useBotStore();
  store.config = config;

  const saveConfigSpy = vi
    .spyOn(notifyClient, 'saveConfig')
    .mockResolvedValue({ ok: true });
  const connectorsSpy = vi
    .spyOn(notifyClient, 'getConnectors')
    .mockResolvedValue(connectorsResponse());
  if (options.connectorsReject !== undefined) {
    // the first connectors load (onMounted) rejects; later retries resolve
    connectorsSpy.mockRejectedValueOnce(new Error(options.connectorsReject));
  }
  vi.spyOn(notifyClient, 'getConfig').mockResolvedValue(config);

  mounted = mount(ConfigView, { global: { plugins: [pinia] } });
  const wrapper = mounted;
  await flushPromises();
  return { wrapper, saveConfigSpy, connectorsSpy };
}

describe('ConfigView banner flow + actions validation (R8)', () => {
  beforeAll(() => {
    // jsdom lacks scrollIntoView: stand the prototype method up for the
    // spec and take it back down afterwards
    scrollCalls = [];
    Object.defineProperty(Element.prototype, 'scrollIntoView', {
      configurable: true,
      writable: true,
      value: function scrollIntoViewRecord(this: Element, options?: ScrollIntoViewOptions): void {
        scrollCalls.push({ target: this, options });
      }
    });
  });

  afterAll(() => {
    delete (Element.prototype as unknown as { scrollIntoView?: unknown }).scrollIntoView;
  });

  afterEach(() => {
    scrollCalls = [];
    if (mounted) {
      mounted.unmount();
      mounted = null;
    }
  });

  describe('save-blocking banner placement + scroll (R8)', () => {
    test('the banner renders inside the Bot & Ollama card, directly above the save row', async () => {
      const config = baseConfig({
        rules: [{ name: 'bad_rule', condition: { notAField: ['x'] } as never, actions: ['log'] }]
      });
      const { wrapper } = await mountConfig(config);
      expect(wrapper.find('.nb-banner-err').exists()).toBe(false); // pristine: no banner

      await wrapper.find('.nb-save-btn').trigger('click');
      await flushPromises();
      await wrapper.vm.$nextTick();

      // lives INSIDE the first card (Bot & Ollama), not at the page bottom
      const banner = wrapper.findAll('ion-card')[0].find('.nb-banner-err');
      expect(banner.exists()).toBe(true);
      expect(banner.text()).toContain('Fix before saving:');
      expect(banner.text()).toContain('unknown condition field');

      // directly above the save row, same card content
      const cardContent = wrapper.findAll('ion-card')[0].element as HTMLElement;
      const bannerEl = banner.element;
      const saveRowEl = cardContent.querySelector('.nb-save-row') as HTMLElement;
      expect(saveRowEl).not.toBeNull();
      const order = Array.prototype.indexOf.call(cardContent.querySelectorAll('*'), bannerEl);
      const saveRowOrder = Array.prototype.indexOf.call(cardContent.querySelectorAll('*'), saveRowEl);
      expect(order).toBeGreaterThan(-1);
      expect(order).toBeLessThan(saveRowOrder);

      // the old page-bottom placement (a direct child of ion-content) is gone
      expect(wrapper.find('.nb-content > .nb-banner-err').exists()).toBe(false);

      wrapper.unmount();
      mounted = null;
    });

    test('a failed save scrolls the banner into view with a reduced-motion aware behavior', async () => {
      const originalMatchMedia = window.matchMedia;
      try {
        const config = baseConfig({
          rules: [{ name: 'bad_rule', condition: { notAField: ['x'] } as never, actions: ['log'] }]
        });
        const { wrapper } = await mountConfig(config);

        // jsdom default: no reduced-motion report -> the behavior stays 'smooth'
        await wrapper.find('.nb-save-btn').trigger('click');
        await flushPromises();
        await wrapper.vm.$nextTick();
        expect(lastScrollCall().options).toEqual({ block: 'nearest', behavior: 'smooth' });
        // the scroll target IS the banner inside the save card
        expect(lastScrollCall().target).toBe(
          wrapper.findAll('ion-card')[0].find('.nb-banner-err').element
        );

        // a positive reduce report degrades the same scroll to 'auto'
        (window as { matchMedia?: unknown }).matchMedia = vi.fn(() => ({ matches: true }));
        const callsAfterFirstScroll = scrollCalls.length;
        await wrapper.find('.nb-save-btn').trigger('click');
        await flushPromises();
        await wrapper.vm.$nextTick();
        expect(scrollCalls.length).toBe(callsAfterFirstScroll + 1);
        expect(lastScrollCall().options).toEqual({ block: 'nearest', behavior: 'auto' });
        wrapper.unmount();
        mounted = null;
      } finally {
        (window as { matchMedia?: unknown }).matchMedia = originalMatchMedia;
      }
    });

    test('when every violation maps to an inline field, the errored input scrolls instead', async () => {
      // a mapped-only failure: preset ollama.timeout = 0 -> the draft check
      // flags the field (mapped, banner stays empty); the fallback scrolls
      // the first .nb-input-bad instead
      const config = baseConfig({ ollama: { url: 'http://127.0.0.1:11434/api/chat', model: 'glm-5.3-flash:cloud', timeout: 0 } });
      const { wrapper } = await mountConfig(config);

      await wrapper.find('.nb-save-btn').trigger('click');
      await flushPromises();
      await wrapper.vm.$nextTick();

      expect(wrapper.find('.nb-save-validation-banner').exists()).toBe(false);
      expect(wrapper.find('.nb-field .nb-input-bad').exists()).toBe(true);
      // the fallback hit the first errored input
      expect(lastScrollCall().options).toEqual({ block: 'nearest', behavior: 'smooth' });
      expect(lastScrollCall().target).toBe(wrapper.find('.nb-field .nb-input-bad').element);
    });
  });

  describe('Actions rows validation (R8)', () => {
    test('an empty command on a command action blocks the save with the inline pair', async () => {
      const config = baseConfig({
        actions: [
          { id: 'clipboard-copy', match_hint: 'copy', type: 'builtin', enabled: true, config: {} },
          { id: 'open-file', match_hint: 'open', type: 'command', enabled: true, config: {} }
        ]
      });
      const { wrapper, saveConfigSpy } = await mountConfig(config);

      await wrapper.find('.nb-save-btn').trigger('click');
      await flushPromises();
      await wrapper.vm.$nextTick();

      // the standard boxed-error pair on the offending row
      const badInput = wrapper.find('.nb-action-row .nb-input-bad');
      expect(badInput.exists()).toBe(true);
      expect(badInput.attributes('aria-invalid')).toBe('true');
      const rowEl = badInput.element.closest('.nb-action-row') as HTMLElement;
      expect(rowEl.querySelector('.nb-field-error')?.textContent).toBe(ACTION_COMMAND_REQUIRED_TEXT);

      // save round-trip is blocked, and with no unmapped errors there is no
      // banner — the fallback scrolls the errored input into view
      expect(saveConfigSpy).not.toHaveBeenCalled();
      expect(wrapper.find('.nb-save-validation-banner').exists()).toBe(false);
      expect(scrollCalls).toHaveLength(1);
      expect(lastScrollCall().target).toBe(badInput.element);

      wrapper.unmount();
      mounted = null;
    });

    test('a filled command passes the round-trip (saveConfig reached, no errors)', async () => {
      const config = baseConfig(); // open-file carries a filled command
      const { wrapper, saveConfigSpy } = await mountConfig(config);

      await wrapper.find('.nb-save-btn').trigger('click');
      await flushPromises();
      await wrapper.vm.$nextTick();

      expect(saveConfigSpy).toHaveBeenCalledTimes(1);
      expect(wrapper.find('.nb-input-bad').exists()).toBe(false);
      expect(wrapper.find('.nb-save-validation-banner').exists()).toBe(false);
      // success: the transient Saved flash on the button
      expect(wrapper.find('.nb-save-btn').text()).toContain('Saved');

      wrapper.unmount();
      mounted = null;
    });
  });

  describe('Connectors banner retry (R8)', () => {
    test('the error banner carries a Retry that re-raises loadConnectors', async () => {
      const { wrapper, connectorsSpy } = await mountConfig(baseConfig(), {
        connectorsReject: 'connectors down'
      });
      // the first load failed in onMounted
      expect(connectorsSpy).toHaveBeenCalledTimes(1);
      const connectorCard = wrapper.findAll('ion-card')[1]; // [0]=Bot & Ollama
      const banner = connectorCard.find('.nb-banner-err');
      expect(banner.exists()).toBe(true);
      expect(banner.text()).toContain('connectors down');
      const retry = banner.find('ion-button');
      expect(retry.text()).toBe('Retry');

      await retry.trigger('click');
      await flushPromises();
      await wrapper.vm.$nextTick();

      expect(connectorsSpy).toHaveBeenCalledTimes(2);
      expect(wrapper.findAll('ion-card')[1].find('.nb-banner-err').exists()).toBe(false);
      // the rows landed
      const rows = wrapper.findAll('.nb-connector-row');
      expect(rows.length).toBe(3);

      wrapper.unmount();
      mounted = null;
    });
  });

  test('page titles render as real headings (role=heading, aria-level=1)', async () => {
    const { wrapper } = await mountConfig(baseConfig());
    const title = wrapper.find('ion-title');
    expect(title.attributes('role')).toBe('heading');
    expect(title.attributes('aria-level')).toBe('1');
    wrapper.unmount();
    mounted = null;
  });
});