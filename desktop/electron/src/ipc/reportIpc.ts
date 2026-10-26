/**
 * notify:report:openExternal / notify:report:reveal — open the generated
 * monthly HTML report in the system browser (shell.openPath) or reveal the
 * file in Finder (shell.showItemInFolder). Paths are validated to live under
 * the repo's reports/ directory.
 */

import { ipcMain, shell } from 'electron';
import * as path from 'path';

import type { PythonProcessManager } from '../processManager';

export interface ShellPathResult {
  success: boolean;
  error?: string;
}

function resolveReportPath(manager: PythonProcessManager, candidate: unknown): { ok: true; path: string } | { ok: false; error: string } {
  if (typeof candidate !== 'string' || candidate.trim() === '') {
    return { ok: false, error: 'path do relatório ausente' };
  }
  const reportsDir = path.resolve(path.join(manager.repoRoot, 'reports'));
  const resolved = path.resolve(candidate);
  if (resolved !== reportsDir && !resolved.startsWith(reportsDir + path.sep)) {
    return { ok: false, error: `path fora do diretório reports/: ${resolved}` };
  }
  return { ok: true, path: resolved };
}

export function registerReportIpc(manager: PythonProcessManager): void {
  ipcMain.handle('notify:report:openExternal', async (_event, candidate: unknown): Promise<ShellPathResult> => {
    const resolved = resolveReportPath(manager, candidate);
    if (resolved.ok === false) return { success: false, error: resolved.error };
    try {
      const result = await shell.openPath(resolved.path);
      // shell.openPath resolves with an empty string on success.
      return result === '' ? { success: true } : { success: false, error: result };
    } catch (err) {
      return { success: false, error: err instanceof Error ? err.message : String(err) };
    }
  });

  ipcMain.handle('notify:report:reveal', (_event, candidate: unknown): ShellPathResult => {
    const resolved = resolveReportPath(manager, candidate);
    if (resolved.ok === false) return { success: false, error: resolved.error };
    try {
      shell.showItemInFolder(resolved.path); // Finder on macOS
      return { success: true };
    } catch (err) {
      return { success: false, error: err instanceof Error ? err.message : String(err) };
    }
  });
}