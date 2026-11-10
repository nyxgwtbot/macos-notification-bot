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

/**
 * Wall-clock age caption for the Status cards: seconds-granularity ages that
 * read live ("4s ago" / "12s ago"), stepping up through min/h/d. Pure in the
 * render clock: the caller passes `nowMs` from the poll tick, so each poll
 * recomputes the label. Empty for a missing/unparseable stamp; a future
 * timestamp (clock skew) clamps to "0s ago".
 */
export function ageLabel(iso: string | null | undefined, nowMs: number): string {
  if (!iso || !Number.isFinite(nowMs)) return '';
  const time = new Date(iso).getTime();
  if (Number.isNaN(time)) return '';
  const seconds = Math.floor(Math.max(0, nowMs - time) / 1000);
  if (seconds < 60) return `${seconds}s ago`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes} min ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours} h ago`;
  return `${Math.floor(hours / 24)} d ago`;
}
