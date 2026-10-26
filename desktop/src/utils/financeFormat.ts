/**
 * BRL formatting + YYYY-MM month arithmetic for the Finance screen.
 * Mirrors notifybot/report.fmt_brl and notifybot.finance.previous_month
 * semantics client-side (tests: tests/unit/financeFormat.spec.ts).
 */

export function formatBrl(value: number | string | null | undefined): string {
  const num = typeof value === 'number' ? value : Number.parseFloat(String(value ?? ''));
  if (!Number.isFinite(num)) return 'R$ 0,00';
  return num.toLocaleString('pt-BR', {
    style: 'currency',
    currency: 'BRL',
    minimumFractionDigits: 2,
    maximumFractionDigits: 2
  });
}

/** '2026-10' -> [2026, 10]; throws on bad month strings. */
export function parseMonth(month: string): [number, number] {
  const match = /^(\d{4})-(\d{2})$/.exec(String(month).trim());
  if (!match) throw new Error(`Mês inválido: ${String(month)}`);
  const year = Number.parseInt(match[1], 10);
  const mon = Number.parseInt(match[2], 10);
  if (mon < 1 || mon > 12) throw new Error(`Mês inválido: ${String(month)}`);
  return [year, mon];
}

export function formatMonth(year: number, mon: number): string {
  return `${String(year).padStart(4, '0')}-${String(mon).padStart(2, '0')}`;
}

/** Next month, calendar-aware across year boundaries. */
export function nextMonth(month: string): string {
  const [year, mon] = parseMonth(month);
  return mon === 12 ? formatMonth(year + 1, 1) : formatMonth(year, mon + 1);
}

/** Previous month, calendar-aware across year boundaries (2026-01 -> 2025-12). */
export function previousMonth(month: string): string {
  const [year, mon] = parseMonth(month);
  return mon === 1 ? formatMonth(year - 1, 12) : formatMonth(year, mon - 1);
}

/** Current local month ('2026-10'). */
export function currentMonth(): string {
  const now = new Date();
  return formatMonth(now.getFullYear(), now.getMonth() + 1);
}

const MONTH_NAMES_PT = [
  'janeiro', 'fevereiro', 'março', 'abril', 'maio', 'junho',
  'julho', 'agosto', 'setembro', 'outubro', 'novembro', 'dezembro'
];

/** '2026-10' -> 'outubro de 2026' (mirrors report.month_label_pt). */
export function monthLabelPt(month: string): string {
  const [year, mon] = parseMonth(month);
  return `${MONTH_NAMES_PT[mon - 1]} de ${year}`;
}

/** Signed MoM delta percentage in PT-BR: '+12,4%' / '-3,1%' (null when undefined). */
export function formatMomDelta(delta: number | null | undefined): string | null {
  if (delta === null || delta === undefined || !Number.isFinite(delta)) return null;
  const text = `${Math.abs(delta).toLocaleString('pt-BR', { maximumFractionDigits: 1 })}%`;
  return `${delta >= 0 ? '+' : '-'}${text}`;
}