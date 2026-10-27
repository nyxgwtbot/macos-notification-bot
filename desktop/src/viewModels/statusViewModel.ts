/**
 * View model helpers for Status / Dashboard view.
 * Default port sync with NOTIFYBOT_API_PORT doc (notifybot/api.py, desktop/electron/src/processManager.ts).
 */

export const DEFAULT_RUNTIME_API_PORT = 4769;

export interface ApiPortStatusInput {
  daemonPort?: number | null;
  adopted?: boolean | null;
  hasHealth?: boolean;
  apiStarted?: boolean;
}

/**
 * Returns formatted port display for the daemon/API section:
 * - "<port> (adopted)" if adopted
 * - "<port>" if running and not adopted
 * - "—" if genuinely unknown
 */
export function formatApiPort(input: ApiPortStatusInput): string {
  let port = input.daemonPort ?? null;
  // Fall back to default runtime port if port is absent but API is known to be running/adopted
  if (port === null && (input.hasHealth || input.adopted || input.apiStarted)) {
    // Fixed runtime constant (sync with NOTIFYBOT_API_PORT doc)
    port = DEFAULT_RUNTIME_API_PORT;
  }
  if (port === null) {
    return '—';
  }
  return input.adopted ? `${port} (adopted)` : `${port}`;
}
