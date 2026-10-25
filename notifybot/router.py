"""Routing engines: deterministic rules and LLM-based action routing (Router v2)."""

import json
import logging
import urllib.request
from typing import List, Optional, Tuple

from notifybot.classifier import clean_json_text

logger = logging.getLogger("notification_bot")


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