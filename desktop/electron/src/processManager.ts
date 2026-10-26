/**
 * PythonProcessManager — spawns, monitors and restarts the two Python
 * children of the desktop app:
 *   - the notification bot daemon:  `<python> bot.py`          (cwd = repo root)
 *   - the JSON API for the renderer: <python> bot.py api --port <P>
 *
 * repo root is resolved as path.resolve(app.getAppPath(), '..', '..') —
 * desktop/electron/* lives two levels below the repo — with the
 * BOT_REPO_PATH env override for future packaged builds.
 *
 * Port strategy (probe-first / adopt-if-healthy): when NOTIFYBOT_API_PORT or
 * the default port 4769 already answers a healthy /api/health, the manager
 * ADOPTS it (e.g. the user started `python3 bot.py api` in a terminal) and
 * no child is spawned. Otherwise it spawns the API server, restarting on a
 * higher port when the preferred one is taken by an unhealthy foreign
 * process.
 */

import { spawn, type ChildProcess } from 'child_process';
import { app } from 'electron';
import * as http from 'http';
import * as path from 'path';

const RING_MAX = 200;
const DEFAULT_API_PORT = 4769;
const SPAWN_PROBE_TRIES = 3;
const PORT_RETRY_OFFSET = 1;
const TERMINATE_GRACE_MS = 3000;

export interface ProcessStatus {
  running: boolean;
  pid: number | null;
  startedAt: string | null;
  uptimeSec: number | null;
  port: number | null;
  adopted: boolean;
  restarts: number;
  lastError: string | null;
}

export interface AppInfo {
  apiPort: number | null;
  apiBaseUrl: string | null;
  repoRoot: string;
  platform: string;
  isElectron: boolean;
}

function isAlive(proc: ChildProcess | null): boolean {
  return !!proc && proc.exitCode === null && proc.signalCode === null;
}

export class PythonProcessManager {
  readonly repoRoot: string;
  readonly pythonBin: string;
  readonly preferredPort: number;

  private apiProc: ChildProcess | null = null;
  private apiPort: number | null = null;
  private apiAdopted = false;
  private apiStartedAt: number | null = null;
  private apiRestarts = 0;
  private apiError: string | null = null;
  private apiStdout: string[] = [];
  private apiStderr: string[] = [];

  private daemonProc: ChildProcess | null = null;
  private daemonStartedAt: number | null = null;
  private daemonRestarts = 0;
  private daemonError: string | null = null;
  private daemonStdout: string[] = [];
  private daemonStderr: string[] = [];

  constructor() {
    const envRoot = (process.env.BOT_REPO_PATH || '').trim();
    this.repoRoot = envRoot ? path.resolve(envRoot) : path.resolve(path.join(app.getAppPath(), '..', '..'));
    this.pythonBin = (process.env.NOTIFYBOT_PYTHON || '/usr/bin/python3').trim();
    const envPort = (process.env.NOTIFYBOT_API_PORT || '').trim();
    this.preferredPort = envPort !== '' ? Number.parseInt(envPort, 10) || DEFAULT_API_PORT : DEFAULT_API_PORT;
  }

  // ---------- ring buffers ----------

  private pushRing(ring: string[], chunk: string): void {
    for (const line of chunk.toString().split(/\r?\n/)) {
      const trimmed = line.trim();
      if (trimmed === '') continue;
      ring.push(trimmed);
      if (ring.length > RING_MAX) ring.splice(0, ring.length - RING_MAX);
    }
  }

  // ---------- health probe (Node http — Electron 26 main has no global fetch) ----------

  probeApi(port: number, timeoutMs = 2000): Promise<boolean> {
    return new Promise<boolean>((resolve) => {
      let settled = false;
      const settle = (value: boolean) => {
        if (!settled) {
          settled = true;
          resolve(value);
        }
      };
      const req = http.get(
        { host: '127.0.0.1', port, path: '/api/health', timeout: timeoutMs },
        (res) => {
          let data = '';
          res.on('data', (chunk) => (data += chunk));
          res.on('end', () => {
            try {
              const parsed = JSON.parse(data);
              settle(parsed != null && parsed.ok === true);
            } catch {
              settle(false);
            }
          });
        }
      );
      req.on('timeout', () => {
        req.destroy();
        settle(false);
      });
      req.on('error', () => settle(false));
    });
  }

  // ---------- spawning ----------

  private spawnPython(args: string[], ringOut: string[], ringErr: string[]): ChildProcess {
    const proc = spawn(this.pythonBin, args, {
      cwd: this.repoRoot,
      env: { ...process.env, PYTHONUNBUFFERED: '1' },
      stdio: ['ignore', 'pipe', 'pipe'],
    });
    proc.stdout?.on('data', (chunk) => this.pushRing(ringOut, chunk as unknown as string));
    proc.stderr?.on('data', (chunk) => this.pushRing(ringErr, chunk as unknown as string));
    return proc;
  }

  /** Spawn/adopt the API server. Resolves with the serving port. */
  async startApi(): Promise<number | null> {
    // Adopt-if-healthy on the preferred port.
    if (await this.probeApi(this.preferredPort)) {
      this.apiAdopted = true;
      this.apiPort = this.preferredPort;
      this.apiError = null;
      this.apiStartedAt = null;
      console.log(`[process] API adotada (já rodando): http://127.0.0.1:${this.apiPort}`);
      return this.apiPort;
    }

    let port = this.preferredPort;
    for (let attempt = 0; attempt < SPAWN_PROBE_TRIES; attempt += 1) {
      this.stopApi(); // clean up a previous failed attempt
      this.apiProc = this.spawnPython(
        ['bot.py', 'api', '--port', String(port)],
        this.apiStdout,
        this.apiStderr
      );
      this.apiProc.on('error', (err) => {
        this.apiError = `spawn falhou: ${err.message}`;
      });
      const exitWatcher = this.apiProc;
      if (exitWatcher) {
        exitWatcher.on('exit', (code, signal) => {
          if (isAlive(exitWatcher)) return;
          if (this.apiProc === exitWatcher) {
            this.apiProc = null;
            this.apiPort = null;
            this.apiStartedAt = null;
            this.apiError = `API saiu (code=${code} signal=${signal ?? '—'})`;
          }
        });
      }
      // Cold python start can take >1s (imports); poll up to ~4s.
      let up = false;
      for (let probeIndex = 0; probeIndex < 8; probeIndex += 1) {
        await new Promise((resolve) => setTimeout(resolve, 500));
        if (await this.probeApi(port)) {
          up = true;
          break;
        }
      }
      if (up) {
        this.apiAdopted = false;
        this.apiPort = port;
        this.apiStartedAt = Date.now();
        this.apiError = null;
        return this.apiPort;
      }
      // Port may be held by a foreign (unhealthy) process — try the next one.
      port = port + PORT_RETRY_OFFSET;
      this.apiError = `porta ${port - PORT_RETRY_OFFSET} não respondeu; tentando porta seguinte`;
    }
    this.apiError = 'não consegui subir/adotar a API em nenhuma porta candidata';
    return null;
  }

  /** Spawn the bot daemon (`<python> bot.py`, cwd = repo root). */
  async startDaemon(): Promise<ProcessStatus> {
    await this.stopDaemon();
    this.daemonProc = this.spawnPython(['bot.py'], this.daemonStdout, this.daemonStderr);
    this.daemonProc.on('error', (err) => {
      this.daemonError = `spawn falhou: ${err.message}`;
    });
    const exitWatcher = this.daemonProc;
    if (exitWatcher) {
      exitWatcher.on('exit', (code, signal) => {
        if (isAlive(exitWatcher)) return;
        if (this.daemonProc === exitWatcher) {
          this.daemonProc = null;
          this.daemonStartedAt = null;
          this.daemonError = `daemon saiu (code=${code} signal=${signal ?? '—'})`;
        }
      });
    }
    this.daemonStartedAt = Date.now();
    this.daemonError = null;
    return this.daemonStatus();
  }

  // ---------- stopping / restarting ----------

  private terminate(proc: ChildProcess, graceMs = TERMINATE_GRACE_MS): Promise<void> {
    if (!isAlive(proc)) return Promise.resolve();
    return new Promise<void>((resolve) => {
      const onExit = () => {
        clearTimeout(timer);
        resolve();
      };
      const timer = setTimeout(() => {
        if (isAlive(proc)) proc.kill('SIGKILL');
        resolve();
      }, graceMs);
      proc.once('exit', onExit);
      proc.kill('SIGTERM');
    });
  }

  stopApi(): void {
    if (this.apiAdopted) return; // never kill a process we did not spawn
    if (this.apiProc && isAlive(this.apiProc)) {
      this.apiProc.kill('SIGTERM');
    }
  }

  stopDaemon(): Promise<ProcessStatus> {
    return this.terminate(this.daemonProc).then(() => this.daemonStatus());
  }

  async restartDaemon(): Promise<ProcessStatus> {
    this.daemonRestarts += 1;
    await this.stopDaemon();
    return this.startDaemon();
  }

  /** Clean shutdown on app quit. */
  shutdown(): void {
    this.stopApi();
    void this.terminate(this.daemonProc);
  }

  // ---------- IPC-facing status ----------

  daemonStatus(): ProcessStatus {
    const running = isAlive(this.daemonProc);
    return {
      running,
      pid: running ? (this.daemonProc as ChildProcess).pid ?? null : null,
      startedAt: this.daemonStartedAt ? new Date(this.daemonStartedAt).toISOString() : null,
      uptimeSec: running && this.daemonStartedAt ? Math.floor((Date.now() - this.daemonStartedAt) / 1000) : null,
      port: null,
      adopted: false,
      restarts: this.daemonRestarts,
      lastError: this.daemonError
    };
  }

  apiStatus(): ProcessStatus {
    const running = this.apiAdopted || isAlive(this.apiProc);
    return {
      running,
      pid: this.apiAdopted ? null : isAlive(this.apiProc) ? (this.apiProc as ChildProcess).pid ?? null : null,
      startedAt: this.apiStartedAt ? new Date(this.apiStartedAt).toISOString() : null,
      uptimeSec: this.apiStartedAt && running ? Math.floor((Date.now() - this.apiStartedAt) / 1000) : null,
      port: this.apiPort,
      adopted: this.apiAdopted,
      restarts: this.apiRestarts,
      lastError: this.apiError
    };
  }

  logs(): { apiStdout: string[]; apiStderr: string[]; daemonStdout: string[]; daemonStderr: string[] } {
    return {
      apiStdout: [...this.apiStdout],
      apiStderr: [...this.apiStderr],
      daemonStdout: [...this.daemonStdout],
      daemonStderr: [...this.daemonStderr]
    };
  }

  appInfo(): AppInfo {
    return {
      apiPort: this.apiPort,
      apiBaseUrl: this.apiPort ? `http://127.0.0.1:${this.apiPort}` : null,
      repoRoot: this.repoRoot,
      platform: process.platform,
      isElectron: true
    };
  }

  /** Current (or re-probed) API port for the IPC proxy. */
  async appPort(): Promise<number | null> {
    if (this.apiPort) return this.apiPort;
    // The user may have started an API server after app boot — re-adopt.
    if (await this.probeApi(this.preferredPort)) {
      this.apiAdopted = true;
      this.apiPort = this.preferredPort;
      this.apiError = null;
      return this.apiPort;
    }
    return null;
  }

  /** Entry: start (or adopt) both children. */
  async start(): Promise<void> {
    await this.startApi();
    await this.startDaemon();
  }
}