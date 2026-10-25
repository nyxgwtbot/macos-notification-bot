#!/usr/bin/env python3
"""
Unit tests for the SVG chart engine (zero-dependency rendering).
"""

import unittest
from decimal import Decimal

from notifybot.report import (
    fmt_brl,
    render_category_bar_chart,
    render_category_donut_chart,
    render_daily_trend_chart,
)

D = Decimal


class TestFmtBrl(unittest.TestCase):
    def test_common_values(self):
        cases = [
            (D("1234.56"), "R$ 1.234,56"),
            (D("200"), "R$ 200,00"),
            (D("0.5"), "R$ 0,50"),
            (D("0"), "R$ 0,00"),
            (D("1234567.89"), "R$ 1.234.567,89"),
            (D("-45.10"), "-R$ 45,10"),
            ("123.4", "R$ 123,40"),
        ]
        for value, expected in cases:
            with self.subTest(value=value):
                self.assertEqual(fmt_brl(value), expected)


class TestCategoryBarChart(unittest.TestCase):
    def test_basic_bars(self):
        svg = render_category_bar_chart({"alimentacao": D("1234.56"), "transporte": D("50.00")})
        self.assertIn("<svg", svg)
        self.assertIn("viewBox", svg)
        self.assertIn("<rect", svg)
        self.assertIn("alimentacao", svg)
        self.assertIn("R$ 1.234,56", svg)
        self.assertIn("R$ 50,00", svg)

    def test_empty_data_placeholder(self):
        svg = render_category_bar_chart({})
        self.assertIn("<svg", svg)
        self.assertIn("Sem gastos", svg)
        # No data bars (bars carry a tooltip title; the placeholder does not)
        self.assertNotIn("<title>", svg)

    def test_labels_escaped(self):
        svg = render_category_bar_chart({'a<b>&"': D("10.00")})
        self.assertNotIn('a<b>&"', svg)
        self.assertIn("&lt;b&gt;", svg)


class TestCategoryDonutChart(unittest.TestCase):
    def test_segments_and_legend(self):
        svg = render_category_donut_chart(
            {"alimentacao": D("60.00"), "transporte": D("40.00"), "outros": D("0.00")}
        )
        self.assertIn("<svg", svg)
        self.assertIn("viewBox", svg)
        self.assertTrue(svg.count("<circle") >= 2)
        self.assertIn("60,0%", svg)  # PT-BR decimal comma percent
        self.assertIn("40,0%", svg)
        self.assertIn("R$ 100,00", svg)  # center total

    def test_single_category_full_circle(self):
        svg = render_category_donut_chart({"compras": D("100.00")})
        self.assertIn("<circle", svg)
        self.assertIn("100,0%", svg)

    def test_zero_total_placeholder(self):
        svg = render_category_donut_chart({"compras": D("0")})
        self.assertIn("<svg", svg)
        self.assertIn("Sem gastos", svg)


class TestDailyTrendChart(unittest.TestCase):
    def test_polyline_and_points(self):
        daily = {
            "2026-10-01": D("50.00"),
            "2026-10-03": D("100.00"),
            "2026-10-10": D("25.00"),
        }
        svg = render_daily_trend_chart(daily, "2026-10")
        self.assertIn("<svg", svg)
        self.assertIn("viewBox", svg)
        self.assertIn("<polyline", svg)
        self.assertIn("<circle", svg)  # per-day markers
        self.assertIn("acumulado", svg)
        self.assertIn("R$ 175,00", svg)

    def test_all_zero_month_placeholder(self):
        svg = render_daily_trend_chart({}, "2026-10")
        self.assertIn("<svg", svg)
        self.assertIn("Sem gastos", svg)
        self.assertNotIn("<polyline", svg)

    def test_invalid_month(self):
        svg = render_daily_trend_chart({"2026-10-01": D("1.00")}, "not-a-month")
        self.assertIn("Mês inválido", svg)

    def test_leap_february(self):
        svg = render_daily_trend_chart({"2024-02-29": D("10.00")}, "2024-02")
        self.assertIn("<polyline", svg)
        self.assertIn("<circle", svg)


if __name__ == "__main__":
    unittest.main()