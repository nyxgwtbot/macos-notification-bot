#!/usr/bin/env python3
"""
Unit tests for the finance plugin (curator + extractor).
Fully offline: LLM extraction is stubbed or urlopen is mocked.
"""

import datetime
import unittest
from decimal import Decimal
from unittest.mock import patch

from notifybot.classifier import LLMClassifier
from notifybot.finance import (
    FinanceCurator,
    FinanceExtractorLLM,
    categorize,
    detect_kind,
    normalize_category,
    parse_brazilian_amount,
)
from notifybot.router import LLMRouter  # noqa: F401  (import sanity)

import json
from unittest.mock import MagicMock


def stub_ollama(content: str):
    mock_response = MagicMock()
    mock_response.read.return_value = json.dumps({"message": {"content": content}}).encode("utf-8")
    mock_response.__enter__.return_value = mock_response
    return patch("urllib.request.urlopen", return_value=mock_response)


class TestParseBrazilianAmount(unittest.TestCase):
    """Brazilian currency regex across common bank-notification formats."""

    def test_currency_formats(self):
        cases = [
            ("Compra aprovada: R$ 1.234,56 no cartão", Decimal("1234.56")),
            ("Compra R$1234,56", Decimal("1234.56")),
            ("Pagamento de R$ 1234.56", Decimal("1234.56")),
            ("R$ 50,00 debitados", Decimal("50")),
            ("R$ 45 na loja", Decimal("45")),
            ("Débito de R$ 1.234 na conta", Decimal("1234")),
            ("Pix de R$0,90", Decimal("0.90")),
        ]
        for text, expected in cases:
            with self.subTest(text=text):
                self.assertEqual(parse_brazilian_amount(text), expected)

    def test_bare_decimal_formats(self):
        cases = [
            ("compra de 30,00 efetuada", Decimal("30")),
            ("saldo disponível 1234.56", Decimal("1234.56")),
            ("valor 1.234,56 confirmado", Decimal("1234.56")),
        ]
        for text, expected in cases:
            with self.subTest(text=text):
                self.assertEqual(parse_brazilian_amount(text), expected)

    def test_no_amount_cases(self):
        negatives = [
            "",
            None,
            "card ending 1234",
            "versão 1.2.3 disponível",
            "contato: 55-1234",
            "saldo insuficiente",
        ]
        for text in negatives:
            with self.subTest(text=text):
                self.assertIsNone(parse_brazilian_amount(text))

    def test_currency_prefix_wins_over_first_bare_token(self):
        # Bare value appears first, but the currency-prefixed one is higher signal.
        self.assertEqual(parse_brazilian_amount("valor 50,00 e taxa de R$ 10,00"), Decimal("10"))

    def test_thousands_precision(self):
        self.assertEqual(parse_brazilian_amount("R$ 12.345.678,90"), Decimal("12345678.90"))


class TestKindAndCategory(unittest.TestCase):
    """Transaction kind and category keyword heuristics."""

    def test_kind_detection(self):
        cases = [
            ("Compra aprovada no cartão final 1234", "gasto"),
            ("Pix enviado para João", "gasto"),
            ("Pagamento de fatura do cartão", "gasto"),
            ("Pix recebido de Maria", "recebimento"),
            ("Estorno de R$ 20,00 processado", "recebimento"),
            ("Você recebeu uma transferência", "recebimento"),
            ("Saldo atualizado na sua conta", "outro"),
        ]
        for text, expected in cases:
            with self.subTest(text=text):
                self.assertEqual(detect_kind(text), expected)

    def test_category_detection(self):
        cases = [
            ("Compra aprovada iFood - Restaurante", "alimentacao"),
            ("Compra no Mercado Livre", "compras"),
            ("Uber para o trabalho", "transporte"),
            ("Compra aprovada Netflix", "assinaturas"),
            ("Farmácia São João", "saude"),
            ("Ingresso do cinema", "lazer"),
            ("Conta de luz vence em 5 dias", "servicos"),
            ("Transferência genérica sem contexto", "outros"),
        ]
        for text, expected in cases:
            with self.subTest(text=text):
                self.assertEqual(categorize(text), expected)

    def test_normalize_category_vocab(self):
        vocab = ["alimentacao", "saude", "outros"]
        self.assertEqual(normalize_category("Saúde", vocab), "saude")
        self.assertEqual(normalize_category("Plano de Saúde", vocab), "outros")
        self.assertEqual(normalize_category("  outros ", vocab), "outros")
        self.assertEqual(normalize_category(None, vocab), "outros")
        self.assertEqual(normalize_category("Xyz", vocab), "outros")


class TestFinanceCurator(unittest.TestCase):
    """Curate pipeline: regex path, LLM fallback path, and safe skips."""

    def notif(self, body="Compra aprovada: R$ 123,45 no iFood — Restaurante", app="com.itau", rec_id=42):
        return {
            "rec_id": rec_id,
            "app": app,
            "uuid": "u1",
            "timestamp": "2026-10-25T10:00:00+00:00",
            "title": "Itaú",
            "subtitle": "banco",
            "body": body,
        }

    def test_curate_regex_path(self):
        curator = FinanceCurator(categories=["alimentacao", "outros"], bank_apps=[])
        result = curator.curate(self.notif(), {"summary": "Compra no iFood"})
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["method"], "regex")
        tx = result["transaction"]
        self.assertEqual(tx["amount"], Decimal("123.45"))
        self.assertEqual(tx["kind"], "gasto")
        self.assertEqual(tx["category"], "alimentacao")
        self.assertEqual(tx["description"], "Compra no iFood")
        self.assertEqual(tx["rec_id"], 42)
        self.assertEqual(tx["bundle"], "com.itau")
        now = datetime.datetime.now().astimezone()
        self.assertTrue(tx["ts"].startswith(now.strftime("%Y-%m-%dT%H:")))
        self.assertIn("R$ 123,45", json.dumps(tx["raw"], default=str))

    def test_curate_bank_apps_restriction(self):
        curator = FinanceCurator(bank_apps=["itau"])
        result = curator.curate(self.notif(app="com.example.messages"))
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["reason"], "app_not_in_bank_apps")
        result_ok = curator.curate(self.notif(app="br.com.itau.app"))
        self.assertEqual(result_ok["status"], "ok")

    def test_curate_llm_fallback_when_regex_fails(self):
        extractor = MagicMock()
        extractor.extract.return_value = {
            "amount": 88.8,
            "kind": "recebimento",
            "category": "Saúde",
            "description": "Transferência recebida",
        }
        curator = FinanceCurator(categories=["saude", "outros"], extractor=extractor)
        result = curator.curate(self.notif(body="Você recebeu uma transferência de Maria"), None)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["method"], "llm")
        tx = result["transaction"]
        self.assertEqual(tx["amount"], Decimal("88.8"))
        self.assertEqual(tx["kind"], "recebimento")
        self.assertEqual(tx["category"], "saude")

    def test_curate_skips_without_amount(self):
        curator = FinanceCurator(extractor=None)
        result = curator.curate(self.notif(body="Sua fatura estava disponível"), None)
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["reason"], "no_amount")

    def test_curate_llm_amount_null_is_skipped(self):
        extractor = MagicMock()
        extractor.extract.return_value = {"amount": None, "kind": "outro", "category": "outros", "description": "d"}
        curator = FinanceCurator(extractor=extractor)
        result = curator.curate(self.notif(body="sem valor monetário aqui"), None)
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["reason"], "no_amount")

    def test_curate_llm_invalid_amount_is_skipped(self):
        extractor = MagicMock()
        extractor.extract.return_value = {"amount": "caro", "kind": "gasto", "category": "outros", "description": "d"}
        curator = FinanceCurator(extractor=extractor)
        result = curator.curate(self.notif(body="sem valor monetário aqui"), None)
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["reason"], "invalid_amount")


class TestFinanceExtractorLLM(unittest.TestCase):
    """Tolerant LLM fallback extraction (mocked HTTP)."""

    def test_extract_fenced_json(self):
        with stub_ollama('```json\n{"amount": 99.9, "kind": "gasto", "category": "outros", "description": "Compra"}\n```'):
            extractor = FinanceExtractorLLM()
            tx = extractor.extract({"app": "b", "title": "t", "subtitle": "", "body": "b"})
        self.assertEqual(tx["amount"], 99.9)
        self.assertEqual(tx["kind"], "gasto")

    def test_extract_network_error_returns_none(self):
        import urllib.error
        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("down")):
            extractor = FinanceExtractorLLM()
            self.assertIsNone(extractor.extract({"app": "b", "title": "t", "subtitle": "", "body": "b"}))

    def test_extract_garbage_returns_none(self):
        with stub_ollama("resposta sem json"):
            extractor = FinanceExtractorLLM()
            self.assertIsNone(extractor.extract({"app": "b", "title": "t", "subtitle": "", "body": "b"}))


if __name__ == "__main__":
    unittest.main()