/**
 * Small time display helpers shared by the Dashboard/Logs screens.
 */

/** ISO timestamp -> compact local string: '26/10 14:32:05' (empty when bad). */
export function formatIsoShort(iso: string | null | undefined): string {
  if (!iso) return '';
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return '';
  const two = (n: number) => String(n).padStart(2, '0');
  return `${two(date.getDate())}/${two(date.getMonth() + 1)} ${two(date.getHours())}:${two(
    date.getMinutes()
  )}:${two(date.getSeconds())}`;
}

/** Seconds -> '2h 03m 41s' / '35s' (empty when not finite). */
export function formatUptime(seconds: number | null | undefined): string {
  const total = Math.max(0, Math.floor(Number(seconds) || 0));
  if (!Number.isFinite(total) || seconds === null || seconds === undefined) return '';
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const secs = total % 60;
  const two = (n: number) => String(n).padStart(2, '0');
  if (hours > 0) return `${hours}h ${two(minutes)}m ${two(secs)}s`;
  if (minutes > 0) return `${minutes}m ${two(secs)}s`;
  return `${secs}s`;
}

/** Human bytes: 0.7 KiB / 1.4 MiB. */
export function formatBytes(bytes: number | null | undefined): string {
  if (bytes === null || bytes === undefined || !Number.isFinite(bytes)) return '';
  if (bytes < 1024) return `${Math.round(bytes)} B`;
  const units = ['KiB', 'MiB', 'GiB'];
  let value = bytes / 1024;
  let unitIndex = 0;
  while (value >= 1024 && unitIndex < units.length - 1) {
    value /= 1024;
    unitIndex += 1;
  }
  return `${value.toFixed(1)} ${units[unitIndex]}`;
}