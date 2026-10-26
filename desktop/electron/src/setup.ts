import type { CapacitorElectronConfig } from '@capacitor-community/electron';
import {
  CapElectronEventEmitter,
  setupCapacitorElectronPlugins,
} from '@capacitor-community/electron';
import chokidar from 'chokidar';
import type { MenuItemConstructorOptions } from 'electron';
import { app, BrowserWindow, Menu, MenuItem, session, shell } from 'electron';
import electronIsDev from 'electron-is-dev';
import electronServe from 'electron-serve';
import windowStateKeeper from 'electron-window-state';
import { join } from 'path';

// Define components for a watcher to detect when the webapp is changed so we
// can reload in Dev mode (mirrors hydranote setup.ts reload watcher).
const reloadWatcher = {
  debouncer: null as ReturnType<typeof setTimeout> | null,
  ready: false,
  watcher: null as chokidar.FSWatcher | null,
};
export function setupReloadWatcher(electronCapacitorApp: ElectronCapacitorApp): void {
  reloadWatcher.watcher = chokidar
    .watch(join(app.getAppPath(), 'app'), {
      ignored: /[/\\]\./,
      persistent: true,
    })
    .on('ready', () => {
      reloadWatcher.ready = true;
    })
    // eslint-disable-next-line @typescript-eslint/no-unused-vars
    .on('all', (_event, _path) => {
      if (reloadWatcher.ready) {
        clearTimeout(reloadWatcher.debouncer as ReturnType<typeof setTimeout>);
        reloadWatcher.debouncer = setTimeout(async () => {
          electronCapacitorApp.getMainWindow().webContents.reload();
          reloadWatcher.ready = false;
          reloadWatcher.watcher = null;
          setupReloadWatcher(electronCapacitorApp);
        }, 1500);
      }
    });
}

// Define our class to manage our app (hydranote ElectronCapacitorApp, slimmed:
// no tray, no splash screen extras, no auto-updater, no media permission hook).
export class ElectronCapacitorApp {
  private MainWindow: BrowserWindow | null = null;
  private CapacitorFileConfig: CapacitorElectronConfig;
  private AppMenuBarMenuTemplate: (MenuItem | MenuItemConstructorOptions)[] = [
    { role: process.platform === 'darwin' ? 'appMenu' : 'fileMenu' },
    { role: 'viewMenu' },
  ];
  private mainWindowState;
  private loadWebApp;
  private customScheme: string;

  constructor(
    capacitorFileConfig: CapacitorElectronConfig,
    appMenuBarMenuTemplate?: (MenuItemConstructorOptions | MenuItem)[]
  ) {
    this.CapacitorFileConfig = capacitorFileConfig;

    this.customScheme = this.CapacitorFileConfig.electron?.customUrlScheme ?? 'capacitor-electron';

    if (appMenuBarMenuTemplate) {
      this.AppMenuBarMenuTemplate = appMenuBarMenuTemplate;
    }

    // Setup our web app loader, this lets us load apps like react, vue, and
    // angular without changing their build chains (electron-serve + custom scheme).
    this.loadWebApp = electronServe({
      directory: join(app.getAppPath(), 'app'),
      scheme: this.customScheme,
    });
  }

  // Helper function to load in the app.
  private async loadMainWindow(thisRef: any): Promise<void> {
    await thisRef.loadWebApp(thisRef.MainWindow);
  }

  // Expose the mainWindow ref for use outside of the class.
  getMainWindow(): BrowserWindow {
    return this.MainWindow;
  }

  getCustomURLScheme(): string {
    return this.customScheme;
  }

  async init(): Promise<void> {
    this.mainWindowState = windowStateKeeper({
      defaultWidth: 1200,
      defaultHeight: 800,
    });
    const isDev = !!electronIsDev;

    // Setup preload script path and construct our main window.
    const preloadPath = join(app.getAppPath(), 'build', 'src', 'preload.js');
    this.MainWindow = new BrowserWindow({
      show: false,
      x: this.mainWindowState.x,
      y: this.mainWindowState.y,
      width: isDev ? 1100 : this.mainWindowState.width,
      height: isDev ? 800 : this.mainWindowState.height,
      webPreferences: {
        nodeIntegration: true,
        contextIsolation: true,
        preload: preloadPath,
      },
    });
    this.mainWindowState.manage(this.MainWindow);

    if (this.CapacitorFileConfig.backgroundColor) {
      this.MainWindow.setBackgroundColor(
        this.CapacitorFileConfig.electron?.backgroundColor as string
      );
    }

    this.MainWindow.on('closed', () => {
      this.MainWindow = null;
    });

    // On macOS the menu bar is part of the OS chrome; on Windows/Linux remove
    // it to avoid the unwanted in-window system menu.
    if (process.platform === 'darwin') {
      Menu.setApplicationMenu(Menu.buildFromTemplate(this.AppMenuBarMenuTemplate));
    } else {
      Menu.setApplicationMenu(null);
    }

    // Load the web app (no splash screen for the MVP).
    await this.loadMainWindow(this);

    // Security - Open external URLs in native browser.
    this.MainWindow.webContents.setWindowOpenHandler((details) => {
      const url = details.url;
      if (url.includes(this.customScheme)) {
        return { action: 'allow' };
      }
      if (url.startsWith('http://') || url.startsWith('https://')) {
        shell.openExternal(url);
      }
      return { action: 'deny' };
    });
    this.MainWindow.webContents.on('will-navigate', (event, newURL) => {
      if (newURL.includes(this.customScheme)) {
        return;
      }
      if (newURL.startsWith('http://') || newURL.startsWith('https://')) {
        event.preventDefault();
        shell.openExternal(newURL);
      }
    });

    // Link electron plugins into the system.
    setupCapacitorElectronPlugins();

    // When the web app is loaded we show the main window.
    this.MainWindow.webContents.on('dom-ready', () => {
      setTimeout(() => {
        if (electronIsDev) {
          this.MainWindow.webContents.openDevTools();
        }
        CapElectronEventEmitter.emit('CAPELECTRON_DeeplinkListenerInitialized', '');
      }, 400);
      this.MainWindow.show();
    });
  }
}

// Set a CSP up for our application based on the custom scheme.
// Slimmed from hydranote: connect-src only 'self' + the custom scheme +
// loopback HTTP (the bot API). No https wildcard, no blob/workers, no
// frame-src (report opens in the system browser via shell.openPath).
export function setupContentSecurityPolicy(customScheme: string): void {
  session.defaultSession.webRequest.onHeadersReceived((details, callback) => {
    const connectSrc = ["'self'", `${customScheme}://*`, 'http://127.0.0.1:*', 'http://localhost:*'].join(' ');

    const csp = [
      `default-src 'self' ${customScheme}://* data:`,
      `script-src 'self' ${customScheme}://* 'unsafe-inline'`,
      `connect-src ${connectSrc}`,
      `style-src 'self' ${customScheme}://* 'unsafe-inline'`,
      `img-src 'self' ${customScheme}://* data:`,
      `font-src 'self' ${customScheme}://* data:`,
    ].join('; ');

    callback({
      responseHeaders: {
        ...details.responseHeaders,
        'Content-Security-Policy': [csp],
      },
    });
  });
}