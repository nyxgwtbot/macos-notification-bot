import { afterEach, beforeEach, describe, expect, test, vi } from 'vitest';

import { ApiError, NotifyClient, notifyClient } from '../../src/services/notifyClient';
import type { NotifyAPI } from '../../src/notify';

// ---- helpers -------------------------------------------------------------

type NotifyAPIKey = keyof NotifyAPI;

function installBridge(bridge: Partial<NotifyAPI> | null): void {
  if (bridge === null) {
    delete window.notifyAPI;
    return;
  }
  window.notifyAPI = bridge as NotifyAPI;
}

async function flushBridgeCall(impl: ReturnType<typeof vi.fn>): Promise<void> {
  impl.mockResolvedValueOnce({ success: true, status: 200, body: JSON.stringify({ ok: true }) });
}

// ---- tests ---------------------------------------------------------------

describe('NotifyClient transport selection', () => {
  beforeEach(() => {
    installBridge(null);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    installBridge(null);
  });

  test('builds direct URLs against the default loopback base in browser-dev mode', async () => {
    const fetchMock = vi.fn(async () => new Response(JSON.stringify({ events: [] }), { status: 200 }));
    vi.stubGlobal('fetch', fetchMock);

    const client = new NotifyClient('http://127.0.0.1:4769');
    await client.getEvents({ limit: 50, since: 12, kind: 'notification' });

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe('http://127.0.0.1:4769/api/events?limit=50&since=12&kind=notification');
    expect(init.method).toBe('GET');
  });

  test('omits empty query params and prefixes the base path', async () => {
    const fetchMock = vi.fn(async () => new Response('{}', { status: 200 }));
    vi.stubGlobal('fetch', fetchMock);

    await new NotifyClient('http://127.0.0.1:4769///').request('GET', '/api/health');

    const [url] = fetchMock.mock.calls[0] as unknown as [string];
    expect(url).toBe('http://127.0.0.1:4769/api/health');
  });

  test('serializes POST bodies as JSON', async () => {
    const fetchMock = vi.fn(async () => new Response('{"ok":true}', { status: 200 }));
    vi.stubGlobal('fetch', fetchMock);

    const client = new NotifyClient();
    const result = await client.postTestNotify();

    expect(result).toEqual({ ok: true });
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe('http://127.0.0.1:4769/api/test-notify');
    expect(init.method).toBe('POST');
    expect(init.body).toBe(JSON.stringify({}));
  });

  test('routes requests through the Electron bridge when notifyAPI exists', async () => {
    const requestMock = vi.fn(async () => ({ success: true, status: 200, body: '{"ok":true,"posted":3}' }));
    installBridge({ api: { request: requestMock as unknown as NotifyAPI['api']['request'] } } as Partial<NotifyAPI>);

    const result = await new NotifyClient().postTestNotify();

    expect(result).toEqual({ ok: true, posted: 3 });
    expect(requestMock).toHaveBeenCalledWith('POST', '/api/test-notify', {});
  });

  test('falls back to fetch when the bridge api object exists but is broken', async () => {
    const brokenBridge = { api: undefined } as unknown as Partial<NotifyAPI>;
    installBridge(brokenBridge);

    const fetchMock = vi.fn(async () => new Response('{"ok":true}', { status: 200 }));
    vi.stubGlobal('fetch', fetchMock);

    const result = await new NotifyClient().postTestNotify();
    expect(result).toEqual({ ok: true });
    expect(fetchMock).toHaveBeenCalled();
  });
});

describe('NotifyClient error mapping', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    installBridge(null);
  });

  test('maps a 400 {error, details} body to ApiError with details', async () => {
    const fetchMock = vi.fn(
      async () =>
        new Response(
          JSON.stringify({ error: 'config inválida', details: ['poll_interval deve ser > 0'] }),
          { status: 400 }
        )
    );
    vi.stubGlobal('fetch', fetchMock);

    await expect(new NotifyClient().saveConfig({} as never)).rejects.toMatchObject({
      name: 'ApiError',
      status: 400,
      message: 'config inválida',
      details: ['poll_interval deve ser > 0']
    });
  });

  test('maps transport-level failures through the bridge error field', async () => {
    const requestMock = vi.fn(async () => ({ success: false, status: null, body: null, error: 'ECONNREFUSED' }));
    installBridge({ api: { request: requestMock as unknown as NotifyAPI['api']['request'] } } as Partial<NotifyAPI>);

    await expect(new NotifyClient().getHealth()).rejects.toBeInstanceOf(ApiError);
    await expect(new NotifyClient().getHealth()).rejects.toMatchObject({
      status: 0,
      message: 'ECONNREFUSED'
    });
  });

  test('throws on non-JSON bodies', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => new Response('<html>not json</html>', { status: 200 }))
    );
    await expect(new NotifyClient().getHealth()).rejects.toMatchObject({
      message: expect.stringContaining('não é JSON válido')
    });
  });

  test('error text fallback works on plain Errors', () => {
    const boom = new ApiError('boom', 400);
    expect(boom.details).toEqual([]);
    expect(boom.name).toBe('ApiError');
    expect(boom).toBeInstanceOf(Error);
    expect(notifyClient.baseUrl).toBe('http://127.0.0.1:4769');
  });
});

describe('client payload wiring (typing smoke)', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  test('getEventsSince omits since=null and passes it when set', async () => {
    const fetchMock = vi.fn(async () => new Response('{"events":[]}', { status: 200 }));
    vi.stubGlobal('fetch', fetchMock);

    const client = new NotifyClient();
    await client.getEventsSince(null);
    const [urlNull] = fetchMock.mock.calls[0] as unknown as [string];
    expect(urlNull).toBe('http://127.0.0.1:4769/api/events');

    await client.getEventsSince(7, { limit: 10 });
    const [urlSince] = fetchMock.mock.calls[1] as unknown as [string];
    expect(urlSince).toBe('http://127.0.0.1:4769/api/events?limit=10&since=7');
  });

  test('flushBridgeCall helper shapes bridge responses (sanity)', async () => {
    const impl = vi.fn();
    await flushBridgeCall(impl);
    expect(await impl()).toEqual({ success: true, status: 200, body: '{"ok":true}' });
    installBridge({ daemon: undefined } as unknown as Partial<NotifyAPI>);
    expect(window.notifyAPI).toBeDefined();
    expect((window.notifyAPI as unknown as NotifyAPIKey).api).toBeUndefined();
  });

  test('#15 getConnectors hits GET /api/connectors and parses rows', async () => {
    const fetchMock = vi.fn(
      async () =>
        new Response(
          JSON.stringify({
            connectors: {
              reminders: { status: 'unknown', granted: false, last_probe: null, hint: 'hint' },
              notes: { status: 'granted', granted: true, last_probe: 'ts', hint: 'connected' },
              system: { status: 'granted', granted: true, last_probe: null, hint: 'no consent required' },
            },
          }),
          { status: 200 }
        )
    );
    vi.stubGlobal('fetch', fetchMock);

    const result = await new NotifyClient().getConnectors();

    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe('http://127.0.0.1:4769/api/connectors');
    expect(init.method).toBe('GET');
    expect(Object.keys(result.connectors)).toEqual(['reminders', 'notes', 'system']);
    expect(result.connectors.system.granted).toBe(true);
    expect(result.connectors.system.hint).toBe('no consent required');
  });

  test('#16 postConnectorProbe posts {domain} and maps a 409 to ApiError', async () => {
    const fetchMock = vi.fn(
      async () =>
        new Response(
          JSON.stringify({ error: "a probe for 'notes' is already running; wait for it to finish" }),
          { status: 409 }
        )
    );
    vi.stubGlobal('fetch', fetchMock);

    await expect(new NotifyClient().postConnectorProbe('notes')).rejects.toMatchObject({
      name: 'ApiError',
      status: 409,
      message: expect.stringContaining('already running'),
    });
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe('http://127.0.0.1:4769/api/connectors/probe');
    expect(init.method).toBe('POST');
    expect(init.body).toBe(JSON.stringify({ domain: 'notes' }));
  });
});