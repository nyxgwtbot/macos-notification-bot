#!/usr/bin/env python3
"""
Unit tests for the SVG chart engine and the finance reporter.
Fully offline: LLM HTTP calls are mocked (or made to fail for fallback tests).
"""

import json
import os
import tempfile
import unittest
import urllib.error
from decimal import Decimal
from unittest.mock import MagicMock, patch

from notifybot.finance import FinanceLedger
from notifybot.report import (
    FinanceReporter,
    fmt_brl,
    month_label_pt,
    render_category_bar_chart,
    render_category_donut_chart,
    render_daily_trend_chart,
    validate_month,
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


def stub_ollama(content: str):
    mock_response = MagicMock()
    mock_response.read.return_value = json.dumps({"message": {"content": content}}).encode("utf-8")
    mock_response.__enter__.return_value = mock_response
    return patch("urllib.request.urlopen", return_value=mock_response)


def make_tx(rec_id, amount, ts, kind="gasto", category="alimentacao", description="Compra"):
    return {
        "rec_id": rec_id,
        "ts": ts,
        "bundle": "com.itau",
        "amount": D(str(amount)),
        "kind": kind,
        "category": category,
        "description": description,
        "raw": {"rec_id": rec_id, "body": f"Compra de R$ {amount}"},
    }


class TestMonthHelpers(unittest.TestCase):
    def test_validate_month(self):
        self.assertEqual(validate_month("2026-10"), "2026-10")
        self.assertEqual(validate_month("2026-01"), "2026-01")
        with self.assertRaises(ValueError):
            validate_month("2026-13")
        with self.assertRaises(ValueError):
            validate_month("2026-1")
        with self.assertRaises(ValueError):
            validate_month("outubro-2026")
        with self.assertRaises(ValueError):
            validate_month("2026-00")

    def test_month_label_pt(self):
        self.assertEqual(month_label_pt("2026-10"), "outubro de 2026")
        self.assertEqual(month_label_pt("2026-01"), "janeiro de 2026")


class TestFinanceReporter(unittest.TestCase):
    """HTML report build, markdown summary, insights (LLM + deterministic fallback)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db_path = os.path.join(self._tmp.name, "finance.db")
        self.ledger = FinanceLedger(self.db_path)
        self.addCleanup(self.ledger.close)
        now_month = validate_month(None)
        prev_month = month_label_prev(now_month)
        self.ledger.insert_transaction(make_tx(1, "123.45", f"{now_month}-05T10:00:00-03:00",
                                               category="alimentacao", description="Compra no iFood"))
        self.ledger.insert_transaction(make_tx(2, "200.00", f"{now_month}-12T10:00:00-03:00",
                                               category="saude", description="Farmácia", kind="recebimento"))
        self.ledger.insert_transaction(make_tx(3, "1234.56", f"{now_month}-20T10:00:00-03:00",
                                               category="servicos", description="Fatura do cartão"))
        # Previous month: one R$ 50,00 expense
        self.ledger.insert_transaction(make_tx(4, "50.00", f"{prev_month}-15T10:00:00-03:00",
                                               category="transporte", description="Uber"))

    def test_report_with_fallback_insights(self):
        reporter = FinanceReporter(ledger=self.ledger, telegram_summary=False, reports_dir=self._tmp.name)
        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("ollama down")):
            result = reporter.build(month=validate_month(None))
        month = result["month"]
        self.assertTrue(os.path.exists(result["html_path"]))
        self.assertEqual(os.path.basename(result["html_path"]), f"{month}.html")
        html_content = result["html"]
        self.assertGreaterEqual(html_content.count("<svg"), 3)
        self.assertIn("Insights", html_content)
        self.assertIn("Gastos por categoria", html_content)
        self.assertIn("Distribuição por categoria", html_content)
        self.assertIn("Evolução diária", html_content)
        self.assertIn("R$ 123,45", html_content)  # table evidence
        md = result["markdown"]
        self.assertIn("Relatório Financeiro", md)
        self.assertIn("R$ 1.558,01", md)  # 123.45+200+1234.56
        self.assertIn("## Insights", md)
        self.assertIn("anterior", md)  # MoM comparison present
        self.assertIn("R$ 50,00", md)  # previous month total present
        self.assertTrue(1 <= len(result["insights"]) <= 5)
        self.assertEqual(result["telegram"], "disabled")

    def test_report_llm_insights_from_fenced_json(self):
        payload = {
            "insights": [
                "Gasto alto com serviços.",
                "Considere revisar assinaturas.",
                "Meta mensal quase atingida.",
                "Reduza transportes em 10%.",
            ]
        }
        with stub_ollama('```json\n' + json.dumps(payload, ensure_ascii=False) + '\n```'):
            reporter = FinanceReporter(ledger=self.ledger, reports_dir=self._tmp.name)
            result = reporter.build(month=validate_month(None))
        self.assertIn("Gasto alto com serviços.", result["insights"])
        self.assertEqual(result["insights"], payload["insights"])

    def test_report_llm_bullet_lines(self):
        content = "- Insight um curto.\n- Insight dois acionável.\n- Insight três.\nTexto solto ignorado."
        with stub_ollama(content):
            reporter = FinanceReporter(ledger=self.ledger, reports_dir=self._tmp.name)
            result = reporter.build(month=validate_month(None))
        self.assertIn("Insight um curto.", result["insights"])
        self.assertIn("Insight dois acionável.", result["insights"])

    def test_fallback_insights_fill_short_llm_answer(self):
        with stub_ollama('- só um bullet curto.'):
            reporter = FinanceReporter(ledger=self.ledger, reports_dir=self._tmp.name)
            result = reporter.build(month=validate_month(None))
        self.assertGreaterEqual(len(result["insights"]), 3)

    def test_report_empty_month(self):
        reporter = FinanceReporter(ledger=self.ledger, reports_dir=self._tmp.name)
        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("down")):
            result = reporter.build(month="2019-07")
        self.assertEqual(result["insights"], [
            "Nenhuma transação registrada neste mês — acompanhe as notificações "
            "bancárias para começar a curadoria de gastos."
        ])
        self.assertIn("Nenhuma transação registrada", result["html"])
        self.assertGreaterEqual(result["html"].count("<svg"), 3)  # empty-state svgs


def month_label_prev(month: str) -> str:
    from notifybot.finance import previous_month
    return previous_month(month)


if __name__ == "__main__":
    unittest.main()