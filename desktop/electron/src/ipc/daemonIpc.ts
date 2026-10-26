/**
 * notify:daemon:* — manage the spawned bot daemon via the Electron bridge.
 */

import { ipcMain } from 'electron';

import type { PythonProcessManager } from '../processManager';

export function registerDaemonIpc(manager: PythonProcessManager): void {
  ipcMain.handle('notify:daemon:status', () => manager.daemonStatus());
  ipcMain.handle('notify:daemon:start', () => manager.startDaemon());
  ipcMain.handle('notify:daemon:stop', () => manager.stopDaemon());
  ipcMain.handle('notify:daemon:restart', () => manager.restartDaemon());
}