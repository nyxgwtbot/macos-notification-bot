/**
 * Type declarations for the Electron bridge exposed by
 * desktop/electron/src/preload.ts via contextBridge.
 * Mirrors the hydranote src/electron.d.ts global Window augmentation pattern.
 */

/** Result of one bot-api:request round-trip through the main process. */
export interface NotifyApiRequestResult {
  success: boolean;
  status?: number;
  body?: string;
  error?: string;
}

/** Daemon status reported by the Electron main process (processManager.ts). */
export interface NotifyDaemonStatus {
  running: boolean;
  pid: number | null;
  startedAt: string | null;
  uptimeSec: number | null;
  port: number | null;
  adopted: boolean;
  restarts: number;
  lastError: string | null;
}

export interface NotifyReportResult {
  success: boolean;
  error?: string;
}

export interface NotifyAppInfo {
  apiPort: number | null;
  apiBaseUrl: string | null;
  repoRoot: string | null;
  platform: string;
  isElectron: boolean;
}

export interface NotifyAPI {
  /** Proxies HTTP requests to the bot API from the Electron main process. */
  api: {
    request: (method: 'GET' | 'POST', path: string, body?: unknown) => Promise<NotifyApiRequestResult>;
  };
  /** Manages the locally spawned bot daemon (`python3 bot.py`). */
  daemon: {
    status: () => Promise<NotifyDaemonStatus>;
    start: () => Promise<NotifyDaemonStatus>;
    stop: () => Promise<NotifyDaemonStatus>;
    restart: () => Promise<NotifyDaemonStatus>;
  };
  /** Opens/reveals generated report files with system tools. */
  report: {
    openExternal: (path: string) => Promise<NotifyReportResult>;
    revealInFinder: (path: string) => Promise<NotifyReportResult>;
  };
  /** Static app/environment info. */
  app: {
    info: () => Promise<NotifyAppInfo>;
  };
}

declare global {
  interface Window {
    notifyAPI?: NotifyAPI;
  }
}

export {};