"""Finance plugin, part 1: transaction curation from bank notifications.

Regex-first extraction of Brazilian currency amounts ("R$ 1.234,56",
"R$ 1234,56", "R$ 1234.56", "50,00", "compra de 30,00"), transaction-kind
and category keyword heuristics, and a tolerant LLM fallback extraction
when the regex finds no monetary value. All money math uses decimal.Decimal.
"""

import datetime
import json
import logging
import re
import unicodedata
import urllib.request
from decimal import Decimal, InvalidOperation
from typing import List, Optional

from notifybot.classifier import clean_json_text

logger = logging.getLogger("notification_bot")

DEFAULT_CATEGORIES = [
    "alimentacao",
    "transporte",
    "compras",
    "saude",
    "lazer",
    "servicos",
    "assinaturas",
    "outros",
]

# Currency-prefixed amount tokens (highest confidence).
# Branch order (longest first): 1.234,56 | 1.234 | 50,00 | 1234.56 | 45
_AMOUNT_TOKEN = (
    r"\d{1,3}(?:\.\d{3})+,\d{1,2}"
    r"|\d{1,3}(?:\.\d{3})+"
    r"|\d+,\d{1,2}"
    r"|\d+\.\d{1,2}"
    r"|\d+"
)
CURRENCY_AMOUNT_RE = re.compile(r"(?:R\$|US\$|\$)\s*(" + _AMOUNT_TOKEN + r")(?![\d.,])")
# Bare amounts require an explicit decimal separator (avoids matching
# integers like "final 1234"); bare thousands with dot ("1.234") are
# intentionally not accepted without a currency prefix.
BARE_AMOUNT_RE = re.compile(
    r"(?<![\d.,])(\d{1,3}(?:\.\d{3})+,\d{1,2}|\d+,\d{1,2}|\d+\.\d{1,2})(?![\d.,])"
)

# Kind detection keywords (accent-stripped, lowercase). "recebimento" is
# checked first so "pagamento recebido"/"estorno" win over "pagamento".
RECEBIMENTO_KEYWORDS = [
    "pix recebido", "pix recebida", "recebido", "recebida", "recebeu", "recebi",
    "recebimento", "recebemos", "estorno", "reembolso", "devolucao", "cashback",
    "deposito", "rendimento", "salario",
]
GASTO_KEYWORDS = [
    "compra", "compras", "pix enviado", "enviado", "enviada", "pagamento",
    "fatura", "debito", "cobranca", "saque", "retirada", "recarga", "boleto",
    "aprovada", "aprovado", "autorizada", "autorizado", "gasto", "parcela",
    "tarifa", "anuidade",
]

# Multi-word brand hints evaluated before the category keyword scan so
# specific names win over generic keywords (e.g. "mercado livre" is
# "compras", not the "mercado" of "alimentacao").
_STRONG_CATEGORY_HINTS = [
    ("mercado livre", "compras"),
    ("mercadolivre", "compras"),
    ("uber eats", "alimentacao"),
    ("google one", "assinaturas"),
    ("amazon prime", "assinaturas"),
    ("plano de saude", "saude"),
    ("conta de luz", "servicos"),
    ("conta de agua", "servicos"),
]

# Ordered category keyword scan (first category with a hit wins).
_CATEGORY_KEYWORDS = [
    ("assinaturas", ["netflix", "spotify", "disney", "hbo", "prime video", "deezer",
                     "icloud", "assinatura", "mensalidade", "plano mensal", "paramount",
                     "youtube premium", "crunchyroll"]),
    ("alimentacao", ["ifood", "rappi", "restaurante", "padaria", "supermercado",
                     "mercado", "hortifruti", "delivery", "lanche", "pizza", "burger",
                     "mcdonald", "burger king", "cafe", "cafeteria", "acai", "sushi"]),
    ("transporte", ["uber", "taxi", "combustivel", "gasolina", "etanol", "diesel",
                    "posto", "metro", "onibus", "estacionamento", "pedagio",
                    "passagem aerea", "99pop"]),
    ("saude", ["farmacia", "drogaria", "clinica", "hospital", "medico", "dentista",
               "exame", "laboratorio", "saude", "psicologo", "nutricionista",
               "academia", "consulta"]),
    ("lazer", ["cinema", "teatro", "show", "steam", "jogo", "games", "playstation",
               "xbox", "nintendo", "parque", "viagem", "hotel", "airbnb", "ingresso"]),
    ("servicos", ["servico", "manutencao", "conserto", "salao", "cabelo",
                  "barbearia", "encanador", "eletricista", "internet", "telefone",
                  "seguro", "curso", "escola"]),
    ("compras", ["compra", "amazon", "americanas", "magalu", "magazine luiza",
                 "shopee", "aliexpress", "loja", "shopping", "shein", "casas bahia",
                 "frete"]),
]


def strip_accents(text: str) -> str:
    """Remove diacritics so keyword matching is accent-insensitive."""
    if not text:
        return ""
    nfkd = unicodedata.normalize("NFD", str(text))
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def _token_to_decimal(token: str) -> Optional[Decimal]:
    """Convert a Brazilian-formatted numeric token into a Decimal."""
    token = token.strip()
    if not token:
        return None
    if "." in token and "," in token:
        # Brazilian convention: dot = thousands, comma = decimal.
        int_part, _, dec_part = token.rpartition(",")
        int_digits = int_part.replace(".", "")
        if int_digits.isdigit() and dec_part.isdigit() and len(dec_part) <= 2:
            return Decimal(f"{int_digits}.{dec_part}")
        return None
    if "," in token:
        int_part, _, dec_part = token.rpartition(",")
        if int_part.isdigit() and dec_part.isdigit() and len(dec_part) <= 2:
            return Decimal(f"{int_part}.{dec_part}")
        return None
    if "." in token:
        parts = token.split(".")
        if len(parts) > 2:
            # Multi-dot: 12.345.678 -> thousands groups; 1.2.3 -> invalid.
            if parts[0].isdigit() and len(parts[0]) in (1, 2, 3) and all(
                p.isdigit() and len(p) == 3 for p in parts[1:]
            ):
                return Decimal("".join(parts))
            return None
        head, tail = parts
        if not head.isdigit() or not tail.isdigit():
            return None
        if len(tail) == 3 and len(head) in (1, 2, 3):
            return Decimal(head + tail)  # "1.234" -> thousands
        if len(tail) in (1, 2):
            return Decimal(f"{head}.{tail}")  # "1234.56" -> decimal
        return None
    if token.isdigit():
        return Decimal(token)
    return None


def parse_brazilian_amount(text: str) -> Optional[Decimal]:
    """
    Extract the first plausible monetary amount from text.

    Currency-prefixed tokens ("R$ 1.234,56") take precedence over bare
    decimal tokens ("compra de 30,00"). Returns None when nothing amounts
    to money (e.g. "card ending 1234" or "versão 1.2.3").
    """
    if not text:
        return None
    for match in CURRENCY_AMOUNT_RE.finditer(str(text)):
        value = _token_to_decimal(match.group(1))
        if value is not None:
            return value
    for match in BARE_AMOUNT_RE.finditer(str(text)):
        value = _token_to_decimal(match.group(1))
        if value is not None:
            return value
    return None


def detect_kind(text: str) -> str:
    """Classify transaction kind: 'gasto' | 'recebimento' | 'outro'."""
    t = strip_accents(text).lower()
    for word in RECEBIMENTO_KEYWORDS:
        if word in t:
            return "recebimento"
    for word in GASTO_KEYWORDS:
        if word in t:
            return "gasto"
    return "outro"


def normalize_category(raw: object, categories: Optional[List[str]] = None) -> str:
    """Map a raw category (e.g. from the LLM) to the configured vocabulary."""
    vocab = list(categories or DEFAULT_CATEGORIES)
    s = strip_accents(str(raw or "")).lower().strip()
    s = re.sub(r"[\s\-]+", "_", s)
    s = s.strip("_-")
    if s in vocab:
        return s
    fallback = "outros" if "outros" in vocab else (vocab[-1] if vocab else "outros")
    return fallback


def categorize(text: str, categories: Optional[List[str]] = None) -> str:
    """Keyword-based category detection over the configured vocabulary."""
    vocab = list(categories or DEFAULT_CATEGORIES)
    t = strip_accents(str(text or "")).lower()

    for hint, category in _STRONG_CATEGORY_HINTS:
        if hint in t and category in vocab:
            return category

    for category, words in _CATEGORY_KEYWORDS:
        if category not in vocab:
            continue
        for word in sorted(words, key=len, reverse=True):
            if word in t:
                return category

    return normalize_category("outros", vocab)


def map_llm_kind(raw: object) -> str:
    """Normalize an LLM-provided kind into gasto/recebimento/outro."""
    s = strip_accents(str(raw or "")).lower().strip()
    if not s:
        return "outro"
    if any(w in s for w in ("receb", "estorno", "entrada", "deposito")):
        return "recebimento"
    if any(w in s for w in ("gasto", "debito", "saida", "enviado", "compra", "pagamento")):
        return "gasto"
    return "outro"


class FinanceExtractorLLM:
    """Tolerant LLM fallback extraction for notifications without regex hits."""

    SYSTEM_PROMPT = (
        "Você extrai transações financeiras de notificações bancárias brasileiras.\n"
        "Analise app, título, subtítulo e corpo da notificação e responda SOMENTE "
        "um objeto JSON válido, sem texto extra, no formato:\n"
        '{"amount": <número em reais, ou null se não houver valor monetário>, '
        '"kind": "gasto"|"recebimento"|"outro", '
        '"category": "<categoria curta em minúsculas, sem acento>", '
        '"description": "<descrição curta em português>"}'
    )

    def __init__(
        self,
        url: str = "http://127.0.0.1:11434/api/chat",
        model: str = "glm-5.3-flash:cloud",
        timeout: float = 15.0,
    ):
        self.url = url
        self.model = model
        self.timeout = float(timeout)

    def extract(self, notif: dict) -> Optional[dict]:
        """Call Ollama and return {amount, kind, category, description} or None."""
        user_content = (
            f"App: {notif.get('app', '')}\n"
            f"Título: {notif.get('title', '')}\n"
            f"Subtítulo: {notif.get('subtitle', '')}\n"
            f"Corpo: {notif.get('body', '')}"
        )
        payload = {
            "model": self.model,
            "stream": False,
            "messages": [
                {"role": "system", "content": self.SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
            ],
        }
        try:
            req_data = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(
                self.url,
                data=req_data,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                resp_json = json.loads(resp.read().decode("utf-8"))
                raw_content = resp_json.get("message", {}).get("content", "")
        except Exception as e:
            logger.warning("Extração financeira via LLM falhou (%s).", e)
            return None

        try:
            parsed = json.loads(clean_json_text(raw_content))
        except Exception:
            logger.warning("Extração financeira via LLM: resposta inválida: %r", raw_content)
            return None
        if not isinstance(parsed, dict):
            return None
        return parsed


class FinanceCurator:
    """
    Curates bank notifications into ledger transactions.

    Regex extraction first; tolerant LLM fallback when no amount is found.
    bank_apps empty = ingest from every app (the router decides what counts
    as finance); when set, only notifications from matching bundle ids are
    ingested.
    """

    def __init__(
        self,
        categories: Optional[List[str]] = None,
        bank_apps: Optional[List[str]] = None,
        extractor: Optional[FinanceExtractorLLM] = None,
    ):
        self.categories = list(categories or DEFAULT_CATEGORIES)
        self.bank_apps = [
            str(b).strip().lower() for b in (bank_apps or []) if str(b).strip()
        ]
        self.extractor = extractor

    def is_bank_app(self, app: object) -> bool:
        if not self.bank_apps:
            return True  # empty allowlist: every app is per router decision
        app_str = str(app or "").lower()
        return any(b in app_str for b in self.bank_apps)

    def _describe(self, notif: dict, classification: Optional[dict]) -> str:
        summary = (classification or {}).get("summary")
        if summary and str(summary).strip():
            return str(summary).strip()[:160]
        title = str(notif.get("title") or "").strip()
        if title:
            return title[:160]
        return str(notif.get("body") or "").strip()[:160]

    def _llm_extract(self, notif: dict) -> Optional[dict]:
        if self.extractor is None:
            return None
        try:
            return self.extractor.extract(notif)
        except Exception as e:
            logger.warning("Extração financeira via LLM falhou: %s", e)
            return None

    def curate(self, notif: dict, classification: Optional[dict] = None) -> dict:
        """
        Build a ledger transaction from a notification.

        Returns a result dict:
        {rec_id, app, status, method, reason?, transaction?}
        transaction = {rec_id, ts, bundle, amount(Decimal), kind, category,
                       description, raw}
        """
        app = notif.get("app", "") or ""
        text = " ".join(
            str(notif.get(k) or "") for k in ("title", "subtitle", "body")
        ).strip()
        result = {
            "rec_id": notif.get("rec_id"),
            "app": app,
            "status": None,
            "method": None,
            "transaction": None,
        }

        if not self.is_bank_app(app):
            result["status"] = "skipped"
            result["reason"] = "app_not_in_bank_apps"
            return result

        amount = parse_brazilian_amount(text)
        kind = category = description = None

        if amount is not None:
            method = "regex"
            kind = detect_kind(text)
            category = categorize(text, self.categories)
            description = self._describe(notif, classification)
        else:
            llm_tx = self._llm_extract(notif)
            if not isinstance(llm_tx, dict):
                result["status"] = "skipped"
                result["reason"] = "no_amount"
                return result
            raw_amount = llm_tx.get("amount")
            if raw_amount is None:
                result["status"] = "skipped"
                result["reason"] = "no_amount"
                return result
            try:
                amount = abs(Decimal(str(raw_amount)))
                if not amount.is_finite():
                    raise InvalidOperation(str(raw_amount))
            except (InvalidOperation, ValueError):
                result["status"] = "skipped"
                result["reason"] = "invalid_amount"
                return result
            method = "llm"
            kind = map_llm_kind(llm_tx.get("kind"))
            category = normalize_category(llm_tx.get("category"), self.categories)
            description = str(llm_tx.get("description") or "").strip()[:200]
            if not description:
                description = self._describe(notif, classification)

        ts_local = datetime.datetime.now().astimezone().isoformat()
        transaction = {
            "rec_id": notif.get("rec_id"),
            "ts": ts_local,
            "bundle": app,
            "amount": amount,
            "kind": kind,
            "category": category,
            "description": description,
            "raw": notif,
        }
        result["status"] = "ok"
        result["method"] = method
        result["transaction"] = transaction
        return result