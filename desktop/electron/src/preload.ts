require('./rt/electron-rt');
//////////////////////////////
// User Defined Preload scripts below

import { contextBridge, ipcRenderer } from 'electron';

/**
 * Expose the notifyAPI bridge to the renderer (mirrors hydranote's
 * preload grouping): api.request proxies HTTP into the main process,
 * daemon manages the local bot daemon, report opens/reveals report files.
 */
contextBridge.exposeInMainWorld('notifyAPI', {
  api: {
    request: (method: string, path: string, body?: unknown) =>
      ipcRenderer.invoke('bot-api:request', { method, path, body })
  },
  daemon: {
    status: () => ipcRenderer.invoke('notify:daemon:status'),
    start: () => ipcRenderer.invoke('notify:daemon:start'),
    stop: () => ipcRenderer.invoke('notify:daemon:stop'),
    restart: () => ipcRenderer.invoke('notify:daemon:restart')
  },
  report: {
    openExternal: (path: string) => ipcRenderer.invoke('notify:report:openExternal', path),
    revealInFinder: (path: string) => ipcRenderer.invoke('notify:report:reveal', path)
  },
  app: {
    info: () => ipcRenderer.invoke('notify:app:info')
  },
  navigate: {
    // G1: menu accelerator clicks push a route string from the main process;
    // the renderer (main.ts) guards it against the known route allowlist.
    onNavigate: (handler: (path: string) => void) => {
      ipcRenderer.on('notify:navigate', (_event, path: string) => handler(path));
    }
  }
});