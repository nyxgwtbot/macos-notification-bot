"""Finance reporting engine.

Part 1 (this module section): zero-dependency inline SVG chart engine —
category bar chart, category share donut chart, and cumulative daily trend
chart. Pure Python standard library (no matplotlib, no pip packages), with
html.escape everywhere and friendly empty-state placeholders.

Part 2: FinanceReporter assembling reports/<YYYY-MM>.html, PT-BR LLM
insights (3-5 bullets), a markdown summary for stdout, and optional
Telegram delivery of the text summary.
"""

import calendar
import datetime
import html
import json
import logging
import os
import urllib.request
from decimal import Decimal
from typing import Dict, List, Optional

from notifybot.classifier import clean_json_text
from notifybot.finance import FinanceLedger, previous_month, strip_accents

logger = logging.getLogger("notification_bot")

# Accessible-contrast categorical palette (Tableau-like, color-blind friendly).
PALETTE = [
    "#4e79a7", "#f28e2b", "#e15759", "#76b7b2", "#59a14f",
    "#edc948", "#b07aa1", "#ff9da7", "#9c755f", "#bab0ac",
]

EMPTY_STATE_TEXT = "Sem gastos registrados neste mês"


def fmt_brl(value: object) -> str:
    """Format a Decimal/number as Brazilian currency: R$ 1.234,56."""
    try:
        d = Decimal(str(value))
    except Exception:
        d = Decimal("0")
    d = d.quantize(Decimal("0.01"))
    negative = d < 0
    text = f"{abs(d):f}"
    whole, _, cents = text.partition(".")
    groups: List[str] = []
    while len(whole) > 3:
        groups.insert(0, whole[-3:])
        whole = whole[:-3]
    groups.insert(0, whole)
    return ("-" if negative else "") + "R$ " + ".".join(groups) + "," + cents


def _empty_svg(width: int, height: int, message: str) -> str:
    """Friendly empty-state placeholder SVG."""
    return (
        f'<svg width="{width}" height="{height}" viewBox="0 0 {width} {height}" '
        f'xmlns="http://www.w3.org/2000/svg" role="img">'
        f'<rect x="0" y="0" width="{width}" height="{height}" rx="8" fill="#f6f7f9"/>'
        f'<text x="{width // 2}" y="{height // 2 + 4}" text-anchor="middle" '
        f'font-family="Helvetica, Arial, sans-serif" font-size="14" fill="#6b7280">'
        f"{html.escape(message)}</text></svg>"
    )


def _escape(value: object) -> str:
    return html.escape(str(value), quote=True)


def render_category_bar_chart(
    category_totals: Dict[str, Decimal],
    width: int = 660,
    bar_height: int = 30,
    gap: int = 12,
    palette: Optional[List[str]] = None,
) -> str:
    """Horizontal bar chart of total spend per category (PT-BR labels)."""
    colors = palette or PALETTE
    items = sorted(
        category_totals.items(), key=lambda kv: kv[1], reverse=True
    )
    items = [(cat, val) for cat, val in items if val > 0] or list(category_totals.items())
    if not items:
        return _empty_svg(width, bar_height + 2 * gap, EMPTY_STATE_TEXT)

    label_w = 150
    value_w = 120
    plot_w = max(width - label_w - value_w, 60)
    top = 10
    height = len(items) * (bar_height + gap) + 2 * gap
    max_val = max((Decimal(str(v)) for _, v in items), default=Decimal("0"))
    max_float = float(max_val) if max_val > 0 else 1.0

    parts = [
        f'<svg width="{width}" height="{height}" viewBox="0 0 {width} {height}" '
        f'xmlns="http://www.w3.org/2000/svg" role="img" '
        f'font-family="Helvetica, Arial, sans-serif">'
    ]
    for index, (category, value) in enumerate(items):
        y = top + index * (bar_height + gap)
        color = colors[index % len(colors)]
        bar_len = max(2.0, plot_w * (float(Decimal(str(value))) / max_float))
        parts.append(
            f'<text x="{label_w - 8}" y="{y + bar_height / 2 + 4}" text-anchor="end" '
            f'font-size="12" fill="#374151">{_escape(category)}</text>'
        )
        parts.append(
            f'<rect x="{label_w}" y="{y}" width="{bar_len:.1f}" height="{bar_height}" '
            f'rx="4" fill="{color}">'
            f"<title>{_escape(category)}: {_escape(fmt_brl(value))}</title></rect>"
        )
        parts.append(
            f'<text x="{label_w + plot_w + 8}" y="{y + bar_height / 2 + 4}" '
            f'font-size="12" fill="#111827">{_escape(fmt_brl(value))}</text>'
        )
    parts.append("</svg>")
    return "".join(parts)


def render_category_donut_chart(
    category_totals: Dict[str, Decimal],
    size: int = 240,
    stroke: int = 34,
    palette: Optional[List[str]] = None,
) -> str:
    """Donut/pie chart of category share with a percentage legend (PT-BR)."""
    colors = palette or PALETTE
    radius = (size - stroke) / 2 - 4
    center = size // 2
    total = sum(Decimal(str(v)) for v in category_totals.values())
    if total <= 0:
        return _empty_svg(size + 220, size, EMPTY_STATE_TEXT)

    circumference = 2 * 3.141592653589793 * radius
    offset = 0.0
    parts = [
        f'<svg width="{size + 220}" height="{size}" viewBox="0 0 {size + 220} {size}" '
        f'xmlns="http://www.w3.org/2000/svg" role="img" '
        f'font-family="Helvetica, Arial, sans-serif">'
        f'<g transform="rotate(-90 {center} {center})">'
    ]
    ordered = sorted(category_totals.items(), key=lambda kv: kv[1], reverse=True)
    legend: List[str] = []
    for index, (category, value) in enumerate(ordered):
        val = Decimal(str(value))
        if val <= 0:
            continue
        frac = float(val) / float(total)
        seg = max(frac * circumference - 1.5, 0.5)  # 1.5px gap between slices
        color = colors[index % len(colors)]
        parts.append(
            f'<circle cx="{center}" cy="{center}" r="{radius:.1f}" fill="none" '
            f'stroke="{color}" stroke-width="{stroke}" stroke-dasharray="{seg:.2f} '
            f'{(circumference - seg):.2f}" stroke-dashoffset="{-offset:.2f}">'
            f"<title>{_escape(category)}: {_escape(fmt_brl(val))} "
            f"({_frac_pct(frac)})</title></circle>"
        )
        legend.append(
            f'<text x="{size + 10}" y="{26 + index * 20}" font-size="12" fill="#374151">'
            f'<tspan fill="{color}" font-size="14">&#9632;</tspan> '
            f"{_escape(category)} — {_frac_pct(frac)}"
            f"</text>"
        )
        offset += frac * circumference
    total_text = (
        f'<text x="{center}" y="{center - 2}" text-anchor="middle" font-size="11" '
        f'fill="#6b7280">total</text>'
        f'<text x="{center}" y="{center + 14}" text-anchor="middle" font-size="13" '
        f'fill="#111827">{_escape(fmt_brl(total))}</text>'
    )
    parts.extend([total_text] + legend)
    parts.append("</g></svg>")
    return "".join(parts)


def _frac_pct(frac: float) -> str:
    return f"{frac * 100:.1f}%".replace(".", ",")


def render_daily_trend_chart(
    daily_totals: Dict[str, Decimal],
    month: str,
    width: int = 680,
    height: int = 260,
    palette: Optional[List[str]] = None,
) -> str:
    """Cumulative daily spend curve for a month (polyline + area path)."""
    colors = palette or PALETTE
    try:
        year, mon = month.split("-")
        year, mon = int(year), int(mon)
        if not (2000 <= year <= 2400 and 1 <= mon <= 12):
            raise ValueError(month)
    except Exception:
        return _empty_svg(width, height, "Mês inválido para o gráfico diário")

    days_in_month = calendar.monthrange(year, mon)[1]
    cumulative: List[float] = []
    total = Decimal("0")
    for day in range(1, days_in_month + 1):
        key = f"{month}-{day:02d}"
        total += Decimal(str(daily_totals.get(key, "0")))
        cumulative.append(float(total))

    if total <= 0:
        return _empty_svg(width, height, EMPTY_STATE_TEXT)

    pad_left, pad_right, pad_top, pad_bottom = 50, 16, 18, 34
    plot_w = width - pad_left - pad_right
    plot_h = height - pad_top - pad_bottom
    max_val = cumulative[-1] if cumulative[-1] > 0 else 1.0

    def x_for(day_index: int) -> float:
        return pad_left + plot_w * (day_index / max(days_in_month - 1, 1))

    def y_for(value: float) -> float:
        return pad_top + plot_h * (1 - value / max_val)

    points = [
        f"{x_for(i):.1f},{y_for(v):.1f}" for i, v in enumerate(cumulative)
    ]
    area = (
        f"M {points[0]} "
        + " ".join(f"L {p}" for p in points[1:])
        + f" L {x_for(len(points) - 1):.1f} {pad_top + plot_h:.1f} "
        + f"L {points[0].split(',')[0]} {pad_top + plot_h:.1f} Z"
    )

    # Day tick labels every ~7 days + last day.
    ticks = list(range(0, days_in_month, 7)) + [days_in_month - 1]
    tick_parts = []
    for day_index in sorted(set(ticks)):
        x = x_for(day_index)
        tick_parts.append(
            f'<line x1="{x:.1f}" y1="{pad_top:.1f}" x2="{x:.1f}" y2="{pad_top + plot_h:.1f}" '
            f'stroke="#e5e7eb" stroke-width="1"/>'
            f'<text x="{x:.1f}" y="{height - 12}" text-anchor="middle" font-size="10" '
            f'fill="#6b7280">{day_index + 1}</text>'
        )

    y_label = f'{_escape(fmt_brl(Decimal(str(total))))}'
    parts = [
        f'<svg width="{width}" height="{height}" viewBox="0 0 {width} {height}" '
        f'xmlns="http://www.w3.org/2000/svg" role="img" '
        f'font-family="Helvetica, Arial, sans-serif">'
        f'<line x1="{pad_left}" y1="{pad_top + plot_h}" x2="{width - pad_right}" '
        f'y2="{pad_top + plot_h}" stroke="#9ca3af" stroke-width="1"/>'
        f'<path d="{area}" fill="{colors[0]}" fill-opacity="0.12" stroke="none"/>'
        f'<polyline fill="none" stroke="{colors[0]}" stroke-width="2.5" '
        f'points="{" ".join(points)}"/>'
    ]
    for i, v in enumerate(cumulative):
        if v > 0:
            parts.append(
                f'<circle cx="{x_for(i):.1f}" cy="{y_for(v):.1f}" r="2.5" fill="{colors[0]}">'
                f"<title>Dia {i + 1}: {_escape(fmt_brl(Decimal(str(v))))}</title></circle>"
            )
    parts.append(
        f'<text x="{pad_left + 6}" y="{pad_top}" font-size="11" fill="#6b7280">'
        f'acumulado: {y_label}</text>'
    )
    parts.extend(tick_parts)
    parts.append("</svg>")
    return "".join(parts)

MONTH_NAMES_PT = {
    1: "janeiro", 2: "fevereiro", 3: "março", 4: "abril", 5: "maio",
    6: "junho", 7: "julho", 8: "agosto", 9: "setembro", 10: "outubro",
    11: "novembro", 12: "dezembro",
}


def validate_month(month: Optional[str]) -> str:
    """Validate an optional 'YYYY-MM' CLI arg; default to the current local month."""
    if month is None:
        return datetime.datetime.now().astimezone().strftime("%Y-%m")
    s = str(month).strip()
    year, dash, mon = s.partition("-")
    if dash != "-" or len(year) != 4 or len(mon) != 2 or not year.isdigit() or not mon.isdigit():
        raise ValueError(f"Mês inválido: {s!r} (use o formato AAAA-MM)")
    year_int, mon_int = int(year), int(mon)
    if not (2000 <= year_int <= 2400 and 1 <= mon_int <= 12):
        raise ValueError(f"Mês inválido: {s!r} (mês deve ser entre 01 e 12)")
    return f"{year_int:04d}-{mon_int:02d}"


def month_label_pt(month: str) -> str:
    """'2026-10' -> 'outubro de 2026'."""
    year, _, mon = month.partition("-")
    return f"{MONTH_NAMES_PT.get(int(mon), mon)} de {int(year)}"


class FinanceReporter:
    """
    Reads ledger aggregates for a month, renders SVG charts into a standalone
    HTML report, generates 3-5 PT-BR insights (LLM with deterministic
    fallback), prints a markdown summary, and optionally delivers it via
    Telegram when config.finance.telegram_summary is enabled.
    """

    def __init__(
        self,
        ledger: FinanceLedger,
        ollama_cfg: Optional[dict] = None,
        categories: Optional[List[str]] = None,
        telegram_summary: bool = False,
        telegram_token: str = "",
        telegram_chat_id: str = "",
        llm_timeout: float = 15.0,
        reports_dir: str = "reports",
    ):
        from notifybot.actions import send_telegram_text

        self.ledger = ledger
        ollama_cfg = ollama_cfg or {}
        self.ollama_url = ollama_cfg.get("url", "http://127.0.0.1:11434/api/chat")
        self.ollama_model = ollama_cfg.get("model", "glm-5.3-flash:cloud")
        self.llm_timeout = float(llm_timeout)
        self.categories = list(categories or [])
        self.telegram_summary = bool(telegram_summary)
        self.send_telegram_text = send_telegram_text
        self.telegram_token = telegram_token or ""
        self.telegram_chat_id = telegram_chat_id or ""
        self.reports_dir = reports_dir

    # ---------- stats ----------

    def compute_stats(self, month: str) -> dict:
        totals = self.ledger.month_totals(month)
        prev_month = previous_month(month)
        prev_totals = self.ledger.month_totals(prev_month)
        prev_total = prev_totals["total"]
        prev_has_data = prev_totals["count"] > 0

        total = totals["total"]
        by_category = totals["by_category"]
        dominant_category = None
        dominant_value = Decimal("0")
        if by_category and total > 0:
            dominant_category, dominant_value = max(
                by_category.items(), key=lambda kv: kv[1]
            )

        by_day = totals["by_day"]
        busiest_day = None
        if by_day:
            busiest_day = max(by_day.items(), key=lambda kv: kv[1])

        max_tx = totals["max_tx"]
        if max_tx:
            biggest = {
                "amount": Decimal(str(max_tx["amount"])),
                "description": max_tx["description"] or max_tx["category"],
                "day": max_tx["ts"][:10],
                "category": max_tx["category"],
            }
        else:
            biggest = None

        mom_delta = None  # Decimal pct variation vs previous month
        if prev_has_data and prev_total > 0 and total is not None:
            mom_delta = (total - prev_total) / prev_total * Decimal("100")

        return {
            "month": month,
            "total": total,
            "count": totals["count"],
            "by_category": by_category,
            "by_day": by_day,
            "biggest": biggest,
            "dominant_category": dominant_category,
            "dominant_value": dominant_value,
            "busiest_day": busiest_day,
            "prev_month": prev_month,
            "prev_total": prev_total,
            "prev_has_data": prev_has_data,
            "mom_delta": mom_delta,
        }

    # ---------- insights ----------

    def fallback_insights(self, stats: dict) -> List[str]:
        """Deterministic PT-BR insights (used when the LLM is unreachable)."""
        insights: List[str] = []
        if stats["count"] == 0:
            return [
                "Nenhuma transação registrada neste mês — acompanhe as notificações "
                "bancárias para começar a curadoria de gastos.",
            ]
        insights.append(
            f"Total do mês: {fmt_brl(stats['total'])} em {stats['count']} transação(ões)."
        )
        if stats["dominant_category"]:
            dom = stats["dominant_category"]
            dom_val = stats["dominant_value"]
            share = (dom_val / stats["total"] * Decimal("100")) if stats["total"] > 0 else Decimal("0")
            insights.append(
                f"Categoria dominante: {dom} ({fmt_brl(dom_val)}, "
                f"{fmt_pct(share)} do total)."
            )
        if stats["biggest"]:
            big = stats["biggest"]
            insights.append(
                f"Maior despesa: {fmt_brl(big['amount'])} — {big['description']} ({big['day']})."
            )
        if stats["busiest_day"]:
            day, value = stats["busiest_day"]
            insights.append(f"Dia mais movimentado: {day} com {fmt_brl(value)}.")
        if stats["prev_has_data"] and stats["mom_delta"] is not None:
            delta_abs = abs(stats["total"] - stats["prev_total"])
            direction = "mais" if stats["mom_delta"] >= 0 else "menos"
            insights.append(
                f"Você gastou {direction} que em {stats['prev_month']}: "
                f"diferença de {fmt_brl(delta_abs)} ({fmt_pct(abs(stats['mom_delta']))})."
            )
        return insights[:5]

    def llm_insights(self, stats: dict) -> Optional[List[str]]:
        """Ask the LLM for 3-5 PT-BR insight bullets seeded with computed stats."""
        lines = [
            f"Mês da análise: {stats['month']}",
            f"Total gasto: {fmt_brl(stats['total'])}",
            f"Número de transações: {stats['count']}",
        ]
        for cat, val in sorted(
            stats["by_category"].items(), key=lambda kv: kv[1], reverse=True
        )[:5]:
            line = f"- {cat}: {fmt_brl(val)}"
            if stats["total"] > 0:
                line += f" ({fmt_pct(val / stats['total'] * Decimal('100'))})"
            lines.append(line)
        if stats["biggest"]:
            big = stats["biggest"]
            lines.append(f"Maior despesa: {fmt_brl(big['amount'])} — {big['description']} ({big['day']})")
        if stats["busiest_day"]:
            day, value = stats["busiest_day"]
            lines.append(f"Dia mais movimentado: {day} com {fmt_brl(value)}")
        if stats["prev_has_data"] and stats["mom_delta"] is not None:
            lines.append(
                f"Total do mês anterior ({stats['prev_month']}): {fmt_brl(stats['prev_total'])} "
                f"(variação de {fmt_pct(stats['mom_delta'])})"
            )
        else:
            lines.append(f"Mês anterior ({stats['prev_month']}): sem dados registrados no ledger")

        system = (
            "Você é um assistente financeiro que escreve em português brasileiro (PT-BR).\n"
            "Com base nas estatísticas do mês abaixo, gere de 3 a 5 bullets curtos, "
            "práticos e acionáveis em PT-BR sobre os gastos do usuário.\n"
            'Responda SOMENTE um JSON válido: {"insights": ["bullet 1", "bullet 2", ...]} '
            "ou como lista com marcadores (- item)."
        )
        user = "Estatísticas do mês:\n" + "\n".join(lines)

        try:
            payload = {
                "model": self.ollama_model,
                "stream": False,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            }
            req_data = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(
                self.ollama_url,
                data=req_data,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=self.llm_timeout) as resp:
                resp_json = json.loads(resp.read().decode("utf-8"))
                raw_content = resp_json.get("message", {}).get("content", "")
        except Exception as e:
            logger.warning("Insights via LLM indisponíveis (%s); usando Insights determinísticos.", e)
            return None

        bullets = _parse_bullets(raw_content)
        if not bullets:
            return None
        return bullets

    # ---------- rendering ----------

    def render_html(self, month: str, stats: dict, insights: List[str]) -> str:
        title = f"Relatório Financeiro — {month_label_pt(month)}"
        parts: List[str] = [
            "<!DOCTYPE html>",
            '<html lang="pt-BR">',
            "<head>",
            '<meta charset="utf-8">',
            '<meta name="viewport" content="width=device-width, initial-scale=1">',
            f"<title>{_escape(title)}</title>",
            "<style>",
            "body{font-family:-apple-system,Helvetica,Arial,sans-serif;margin:0;background:#f6f7f9;color:#111827}",
            ".wrap{max-width:900px;margin:0 auto;padding:24px}",
            "h1{font-size:24px} h2{font-size:18px;margin-top:32px}",
            ".card{background:#fff;border:1px solid #e5e7eb;border-radius:10px;padding:16px;margin-bottom:16px}",
            "table{border-collapse:collapse;width:100%}",
            "td,th{border-bottom:1px solid #e5e7eb;padding:8px;text-align:left;font-size:13px}",
            "th{background:#f6f7f9}",
            ".num{white-space:nowrap;text-align:right}",
            ".hint{color:#6b7280;font-size:12px}",
            "</style>",
            "</head>",
            "<body><div class='wrap'>",
            f"<h1>{_escape(title)}</h1>",
            "<div class='card'><strong>Resumo do mês</strong><br>",
            f"Total: <strong>{_escape(fmt_brl(stats['total']))}</strong> em "
            f"{stats['count']} transação(ões).",
        ]
        if stats["prev_has_data"] and stats["mom_delta"] is not None:
            direction = "acima" if stats["mom_delta"] >= 0 else "abaixo"
            parts.append(
                f" — {fmt_pct(abs(stats['mom_delta']))} {direction} do mês anterior "
                f"({stats['prev_month']}: {_escape(fmt_brl(stats['prev_total']))})"
            )
        elif not stats["prev_has_data"]:
            parts.append(f" — mês anterior ({stats['prev_month']}) sem dados registrados")
        parts.append("</div>")

        parts.append("<h2>Gastos por categoria</h2><div class='card'>")
        parts.append(render_category_bar_chart(
            {k: v for k, v in stats["by_category"].items()},
        ))
        parts.append("</div>")

        parts.append("<h2>Distribuição por categoria</h2><div class='card'>")
        parts.append(render_category_donut_chart(dict(stats["by_category"])))
        parts.append("</div>")

        parts.append("<h2>Evolução diária (acumulado)</h2><div class='card'>")
        parts.append(render_daily_trend_chart(dict(stats["by_day"]), month))
        parts.append("</div>")

        parts.append("<h2>Insights</h2><div class='card'><ul>")
        for bullet in insights:
            parts.append(f"<li>{_escape(bullet)}</li>")
        parts.append("</ul></div>")

        # Transaction detail table (useful evidence for smoke checks)
        parts.append("<h2>Transações</h2><div class='card'><table>")
        parts.append("<tr><th>Dia</th><th>Valor</th><th>Tipo</th><th>Categoria</th><th>Descrição</th></tr>")
        records = self.ledger.month_records(month)
        for rec in records:
            parts.append(
                "<tr>"
                f"<td>{_escape(rec['ts'][:10])}</td>"
                f"<td class='num'>{_escape(fmt_brl(Decimal(str(rec['amount']))))}</td>"
                f"<td>{_escape(rec['kind'])}</td>"
                f"<td>{_escape(rec['category'])}</td>"
                f"<td>{_escape(rec['description'] or '')}</td>"
                "</tr>"
            )
        if not records:
            parts.append("<tr><td colspan='5' class='hint'>Nenhuma transação registrada.</td></tr>")
        parts.append("</table></div>")
        parts.append(
            f"<p class='hint'>Gerado por notifybot em "
            f"{_escape(datetime.datetime.now().astimezone().strftime('%d/%m/%Y %H:%M'))} "
            "— dados do ledger local (finance.db).</p>"
        )
        parts.append("</div></body></html>")
        return "\n".join(parts)

    def markdown_summary(self, month: str, stats: dict, insights: List[str]) -> str:
        lines = [
            f"# Relatório Financeiro — {month_label_pt(month)}",
            "",
            f"**Total:** {fmt_brl(stats['total'])} · **Transações:** {stats['count']}",
        ]
        if stats["prev_has_data"] and stats["mom_delta"] is not None:
            direction = "acima" if stats["mom_delta"] >= 0 else "abaixo"
            lines.append(
                f"**Mês anterior** ({stats['prev_month']}): {fmt_brl(stats['prev_total'])} "
                f"— {fmt_pct(abs(stats['mom_delta']))} {direction}"
            )
        elif not stats["prev_has_data"]:
            lines.append(f"**Mês anterior** ({stats['prev_month']}): sem dados registrados")
        lines += ["", "## Por categoria"]
        if stats["by_category"]:
            for cat, val in sorted(
                stats["by_category"].items(), key=lambda kv: kv[1], reverse=True
            ):
                share = (val / stats["total"] * Decimal("100")) if stats["total"] > 0 else Decimal("0")
                lines.append(f"- {cat}: {fmt_brl(val)} ({fmt_pct(share)})")
        else:
            lines.append("- Nenhuma categoria com gastos.")
        lines += ["", "## Insights"]
        for bullet in insights:
            lines.append(f"- {bullet}")
        return "\n".join(lines)

    def send_summary_telegram(self, markdown: str) -> str:
        """Deliver the text summary via Telegram when enabled; never raises."""
        if not self.telegram_summary:
            return "disabled"
        status = self.send_telegram_text(self.telegram_token, self.telegram_chat_id, markdown)
        logger.info("Resumo financeiro via Telegram: %s", status)
        return status

    def build(self, month: Optional[str] = None) -> dict:
        """Render + write reports/<month>.html and return the summary payload."""
        month = validate_month(month)
        stats = self.compute_stats(month)
        insights = self.llm_insights(stats)
        if not insights:
            insights = self.fallback_insights(stats)
        # Ensure 3-5 bullets; fill with deterministic ones when the LLM fell short.
        if stats["count"] > 0 and len(insights) < 3:
            for extra in self.fallback_insights(stats):
                if extra not in insights:
                    insights.append(extra)
                if len(insights) >= 3:
                    break
        insights = insights[:5]

        html_content = self.render_html(month, stats, insights)
        os.makedirs(self.reports_dir, exist_ok=True)
        html_path = os.path.join(self.reports_dir, f"{month}.html")
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(html_content)

        markdown = self.markdown_summary(month, stats, insights)
        telegram_status = self.send_summary_telegram(markdown)

        return {
            "month": month,
            "html_path": html_path,
            "html": html_content,
            "markdown": markdown,
            "insights": insights,
            "telegram": telegram_status,
            "stats": {
                "total": stats["total"],
                "count": stats["count"],
                "prev_total": stats["prev_total"],
                "mom_delta": stats["mom_delta"],
            },
        }


def _parse_bullets(raw_content: str) -> List[str]:
    """Tolerantly parse insight bullets: JSON {'insights': [...]} or marker lines."""
    if not raw_content:
        return []
    bullets: List[str] = []
    try:
        parsed = json.loads(clean_json_text(raw_content))
        if isinstance(parsed, dict):
            items = parsed.get("insights")
            if isinstance(items, list):
                bullets = [str(i).strip() for i in items if str(i).strip()]
    except Exception:
        pass
    if not bullets:
        for line in str(raw_content).splitlines():
            t = line.strip()
            if not t:
                continue
            for marker in ("- ", "• ", "* ", "– "):
                if t.startswith(marker):
                    t = t[len(marker):].strip()
                    break
            else:
                # Numbered list "1. ..." or plain continuation lines are ignored
                continue
            if t:
                bullets.append(t)
    return [b for b in bullets if b][:5]


def fmt_pct(value: Decimal) -> str:
    """Format a Decimal percentage in PT-BR: '12,3%'."""
    return f"{float(value):.1f}%".replace(".", ",")
