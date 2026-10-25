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