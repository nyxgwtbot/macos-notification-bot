/**
 * bot-api:request — proxy that forwards renderer HTTP calls to the bot API.
 * Runs in the Electron MAIN process on the Node `http` module (Electron 26
 * main is Node 16 — no global fetch), mirroring hydranote's web:fetch IPC
 * pattern. Default timeout 60s; agent turns get a 150s budget (a worst-case
 * turn is a ~90s agent budget plus one in-flight 30s Ollama call — see
 * notifybot/agent.py run_turn).
 */

import { ipcMain } from 'electron';
import * as http from 'http';

import type { PythonProcessManager } from '../processManager';

const PROXY_TIMEOUT_MS = 60000;
// Agent-path timeout (/api/agent/*): turn budget 90s + one in-flight 30s
// Ollama call exceeds the 60s default, so agent paths get 150s. The UI
// recovers a finished-but-unresponsive turn via GET /api/agent/session/<id>.
const AGENT_PROXY_TIMEOUT_MS = 150000;

export interface BotApiHttpRequest {
  method: 'GET' | 'POST';
  path: string;
  body?: unknown;
}

export interface BotApiHttpResult {
  success: boolean;
  status: number | null;
  body: string | null;
  error: string | null;
}

function validateRequest(
  req: unknown
): { ok: true; value: BotApiHttpRequest } | { ok: false; error: string } {
  const candidate = req as BotApiHttpRequest | null;
  if (!candidate || typeof candidate !== 'object') return { ok: false, error: 'request inválido' };
  if (candidate.method !== 'GET' && candidate.method !== 'POST') {
    return { ok: false, error: `método não suportado: ${String(candidate.method)}` };
  }
  if (typeof candidate.path !== 'string' || !candidate.path.startsWith('/')) {
    return { ok: false, error: 'path inválido (deve começar com "/")' };
  }
  if (candidate.path.length > 2048) {
    return { ok: false, error: 'path longo demais' };
  }
  if (candidate.body !== undefined && typeof candidate.body === 'function') {
    return { ok: false, error: 'body inválido (função não serializável)' };
  }
  return { ok: true, value: { method: candidate.method, path: candidate.path, body: candidate.body } };
}

/** One http.request round-trip to 127.0.0.1:<port> (60s default, 150s for
 * agent paths — see AGENT_PROXY_TIMEOUT_MS). */
function proxyOnce(port: number, req: BotApiHttpRequest): Promise<BotApiHttpResult> {
  return new Promise((resolve, reject) => {
    const payload = req.method === 'POST' && req.body !== undefined ? JSON.stringify(req.body) : null;
    const headers: Record<string, string | number> = {};
    if (payload !== null) {
      headers['Content-Type'] = 'application/json; charset=utf-8';
      headers['Content-Length'] = Buffer.byteLength(payload);
    }
    const timeoutMs = req.path.startsWith('/api/agent/') ? AGENT_PROXY_TIMEOUT_MS : PROXY_TIMEOUT_MS;
    const request = http.request(
      { host: '127.0.0.1', port, path: req.path, method: req.method, headers, timeout: timeoutMs },
      (response) => {
        const chunks: Buffer[] = [];
        response.on('data', (chunk) => chunks.push(chunk as Buffer));
        response.on('end', () => {
          resolve({
            success: true,
            status: response.statusCode ?? 0,
            body: Buffer.concat(chunks).toString('utf-8'),
            error: null
          });
        });
      }
    );
    request.setTimeout(timeoutMs, () => {
      reject(new Error(`timeout da chamada ao bot API (${Math.round(timeoutMs / 1000)}s)`));
    });
    request.on('error', (err) => reject(err));
    if (payload !== null) request.write(payload);
    request.end();
  });
}

export function registerBotApiIpc(manager: PythonProcessManager): void {
  ipcMain.handle('bot-api:request', async (_event, rawRequest: unknown): Promise<BotApiHttpResult> => {
    const validated = validateRequest(rawRequest);
    if (validated.ok === false) {
      return { success: false, status: null, body: null, error: validated.error };
    }
    const port = manager.probeApi === undefined ? null : (await manager.appPort());
    if (!port) {
      return {
        success: false,
        status: null,
        body: null,
        error: 'bot API não está respondendo (reinicie o app ou inicie `python3 bot.py api`)'
      };
    }
    try {
      return await proxyOnce(port, validated.value);
    } catch (err) {
      return {
        success: false,
        status: null,
        body: null,
        error: err instanceof Error ? err.message : String(err)
      };
    }
  });
}