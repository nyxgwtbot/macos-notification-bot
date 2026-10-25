"""Routing engines: deterministic rules and LLM-based action routing (Router v2).

Two-stage pipeline:
1. Deterministic ``config.rules`` evaluate first (legacy, overrides).
2. Non-matching notifications go to the LLM router, which picks one action id
   from the enabled actions registry (id + match_hint only).
3. Low confidence (< router.min_confidence), unknown ids, or LLM failures fall
   back to the "log" builtin.
"""

import json
import logging
import urllib.request
from typing import List, Optional, Tuple

from notifybot.classifier import clean_json_text

logger = logging.getLogger("notification_bot")

# Layer values recorded in every notification event of logs/events.jsonl.
LAYER_RULE = "rule"
LAYER_LLM_ROUTER = "llm_router"
LAYER_FALLBACK = "fallback_in_fallback"


class RuleRouter:
    """Evaluates routing rules top-down; first match wins; fallthrough default ['log']."""

    def __init__(self, rules: Optional[List[dict]] = None):
        self.rules = rules or []

    def match(self, notif: dict, classification: dict) -> Optional[Tuple[str, List[str]]]:
        """Return (rule_name, actions) of the first matching rule, or None."""
        for rule in self.rules:
            name = rule.get("name", "unnamed")
            cond = rule.get("condition", {})
            actions = rule.get("actions", ["log"])

            if self._matches(cond, notif, classification):
                return name, actions
        return None

    def evaluate(self, notif: dict, classification: dict) -> Tuple[str, List[str]]:
        """Backward-compatible Phase 1 API: first match, else default fallthrough."""
        matched = self.match(notif, classification)
        if matched is not None:
            return matched
        return "default_fallthrough", ["log"]

    def _matches(self, cond: dict, notif: dict, classification: dict) -> bool:
        if not cond:
            return True

        for key, expected in cond.items():
            actual = classification.get(key)
            if actual is None:
                actual = notif.get(key)

            if actual is None:
                return False

            actual_str = str(actual).lower()
            if isinstance(expected, list):
                if actual_str not in [str(x).lower() for x in expected]:
                    return False
            else:
                if actual_str != str(expected).lower():
                    return False

        return True


class LLMRouter:
    """
    LLM-based action router (Router v2, second stage).

    Builds a structured prompt from ONLY the enabled actions' (id, match_hint),
    asks the model for JSON {action_id, confidence, payload} and validates the
    decision. Low confidence, unknown ids, or any failure falls back to the
    deterministic "log" builtin.
    """

    SYSTEM_PROMPT = (
        "Você é o roteador de ações de um assistente de notificações do macOS.\n"
        "Dada uma notificação (app, título, subtítulo, corpo) e sua classificação, "
        "escolha a ação MAIS adequada dentre as ações disponíveis listadas.\n"
        'Responda SOMENTE um objeto JSON válido, sem texto extra, no formato:\n'
        '{"action_id": "<id exato de uma ação listada>", "confidence": <número entre 0.0 e 1.0>, '
        '"payload": <objeto opcional com dados extras úteis>}\n'
        "Regras:\n"
        "- action_id deve ser exatamente um dos ids disponíveis.\n"
        '- Se nenhuma ação específica se aplicar, escolha "log".\n'
        "- confidence exprime sua certeza da escolha (0.0 a 1.0)."
    )

    def __init__(
        self,
        actions_registry: Optional[List[dict]] = None,
        url: str = "http://127.0.0.1:11434/api/chat",
        model: str = "glm-5.3-flash:cloud",
        timeout: float = 15.0,
        min_confidence: float = 0.5,
    ):
        self.registry: List[dict] = list(actions_registry or [])
        self.url = url
        self.model = model
        self.timeout = float(timeout)
        self.min_confidence = float(min_confidence)

    def enabled_actions(self) -> List[dict]:
        """Return [{'id', 'match_hint'}] for enabled registry entries only."""
        enabled = []
        for entry in self.registry:
            if not entry.get("enabled", True):
                continue
            enabled.append(
                {
                    "id": str(entry.get("id", "")).strip(),
                    "match_hint": str(entry.get("match_hint", "")),
                }
            )
        return [a for a in enabled if a["id"]]

    def fallback(self, reason: str, confidence: Optional[float] = None) -> dict:
        """Deterministic fallback decision: route to the 'log' builtin."""
        return {
            "layer": LAYER_FALLBACK,
            "actions": ["log"],
            "rule_matched": None,
            "action_id": "log",
            "confidence": confidence,
            "payload": {},
            "reason": reason,
        }

    def build_prompt(self, notif: dict, classification: dict) -> Tuple[str, str]:
        """Build (system, user) prompt; user part lists enabled actions only."""
        action_lines = []
        for action in self.enabled_actions():
            hint = action["match_hint"].replace("\n", " ").strip()
            action_lines.append(f'- "{action["id"]}": {hint}')
        actions_block = "\n".join(action_lines) or '- "log": Ação padrão de registro.'

        user = (
            f"Ações disponíveis:\n{actions_block}\n\n"
            "Notificação:\n"
            f"App: {notif.get('app', '')}\n"
            f"Título: {notif.get('title', '')}\n"
            f"Subtítulo: {notif.get('subtitle', '')}\n"
            f"Corpo: {notif.get('body', '')}\n\n"
            "Classificação:\n"
            f"Categoria: {classification.get('category', '')} | "
            f"Urgência: {classification.get('urgency', '')} | "
            f"Resumo: {classification.get('summary', '')}\n\n"
            "Responda com o JSON da ação escolhida."
        )
        return self.SYSTEM_PROMPT, user

    def _parse_confidence(self, raw: object) -> float:
        try:
            conf = float(raw)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return 0.0
        if conf > 1.0:
            conf = conf / 100.0 if conf <= 100.0 else 0.0
        return max(0.0, min(1.0, conf))

    def route(self, notif: dict, classification: dict) -> dict:
        """Ask the LLM which enabled action to run; validate and fall back safely."""
        enabled = self.enabled_actions()
        if not enabled:
            return self.fallback("no_enabled_actions")

        system, user = self.build_prompt(notif, classification)
        payload = {
            "model": self.model,
            "stream": False,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
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
            logger.warning("Roteamento por LLM falhou (%s); usando fallback para 'log'.", e)
            return self.fallback("llm_error")

        cleaned = clean_json_text(raw_content)
        try:
            parsed = json.loads(cleaned)
        except Exception:
            return self.fallback("malformed_response")
        if not isinstance(parsed, dict):
            return self.fallback("malformed_response")

        action_id = str(parsed.get("action_id", "")).strip()
        confidence = self._parse_confidence(parsed.get("confidence"))
        payload_out = parsed.get("payload")
        if not isinstance(payload_out, dict):
            payload_out = {}

        if not action_id:
            return self.fallback("missing_action_id", confidence)
        enabled_ids = {a["id"] for a in enabled}
        if action_id not in enabled_ids:
            return self.fallback("unknown_action", confidence)
        if confidence < self.min_confidence:
            return self.fallback("low_confidence", confidence)

        return {
            "layer": LAYER_LLM_ROUTER,
            "actions": [action_id],
            "rule_matched": None,
            "action_id": action_id,
            "confidence": confidence,
            "payload": payload_out,
        }


class Router:
    """
    Two-stage Router v2 pipeline.

    Deterministic config.rules evaluate first (legacy behavior, overrides);
    non-matching notifications are decided by the LLM router. The composite
    returns a routing decision dict consumed by ActionDispatcher.
    """

    def __init__(self, rules: Optional[List[dict]] = None, llm_router: Optional[LLMRouter] = None):
        self.rule_router = RuleRouter(rules=rules)
        self.llm_router = llm_router

    def route(self, notif: dict, classification: dict) -> dict:
        matched = self.rule_router.match(notif, classification)
        if matched is not None:
            rule_name, actions = matched
            return {
                "layer": LAYER_RULE,
                "actions": list(actions),
                "rule_matched": rule_name,
                "action_id": None,
                "confidence": None,
                "payload": {},
            }
        if self.llm_router is None:
            decision = {
                "layer": LAYER_FALLBACK,
                "actions": ["log"],
                "rule_matched": None,
                "action_id": "log",
                "confidence": None,
                "payload": {},
                "reason": "no_llm_router",
            }
            return decision
        return self.llm_router.route(notif, classification)