import type { CapacitorElectronConfig } from '@capacitor-community/electron';
import { getCapacitorElectronConfig } from '@capacitor-community/electron';
import type { MenuItemConstructorOptions } from 'electron';
import { app, ipcMain, Menu, MenuItem } from 'electron';
import electronIsDev from 'electron-is-dev';
import unhandled from 'electron-unhandled';

import {
  ElectronCapacitorApp,
  setupContentSecurityPolicy,
  setupReloadWatcher
} from './setup';
import { PythonProcessManager } from './processManager';
import { registerBotApiIpc } from './ipc/botApiIpc';
import { registerDaemonIpc } from './ipc/daemonIpc';
import { registerReportIpc } from './ipc/reportIpc';

// Graceful handling of unhandled errors.
unhandled();

// Define our menu templates (no tray for the MVP).
const appMenuBarMenuTemplate: (MenuItemConstructorOptions | MenuItem)[] = [
  { role: process.platform === 'darwin' ? 'appMenu' : 'fileMenu' },
  { role: 'editMenu' },
  { role: 'viewMenu' }
];

// Get Config options from capacitor.config
const capacitorFileConfig: CapacitorElectronConfig = getCapacitorElectronConfig();

// Initialize our app.
const myCapacitorApp = new ElectronCapacitorApp(capacitorFileConfig, appMenuBarMenuTemplate);

// The python child-process manager (API server + bot daemon).
const manager = new PythonProcessManager();

// If we are in Dev mode, use the file watcher components.
if (electronIsDev) {
  setupReloadWatcher(myCapacitorApp);
}

// Quit flow: sigterm both Python children before electron exits.
let shutdownStarted = false;
app.on('before-quit', () => {
  if (!shutdownStarted) {
    shutdownStarted = true;
    manager.shutdown();
  }
});

app.on('window-all-closed', () => {
  // macOS keeps the conventional single-window app lifecycle: Cmd+Q quits.
  if (process.platform !== 'darwin') {
    app.quit();
  }
});

app.on('activate', async () => {
  const mainWindow = myCapacitorApp.getMainWindow();
  if (mainWindow && !mainWindow.isDestroyed()) {
    mainWindow.show();
    mainWindow.focus();
  }
});

// Run Application
(async () => {
  // Wait for electron app to be ready.
  await app.whenReady();
  // Security - Set Content-Security-Policy (loopback-only connect-src).
  setupContentSecurityPolicy(myCapacitorApp.getCustomURLScheme());
  // Initialize our app, build windows, and load content.
  await myCapacitorApp.init();

  // Start (or adopt) the python children: JSON API + bot daemon.
  await manager.start();

  // Wire the IPC surface: api proxy / daemon control / report shell ops /
  // app info (apiPort etc. for the renderer).
  registerBotApiIpc(manager);
  registerDaemonIpc(manager);
  registerReportIpc(manager);
  ipcMain.handle('notify:app:info', () => manager.appInfo());
  ipcMain.handle('notify:app:logs', () => manager.logs());
})();