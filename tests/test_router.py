#!/usr/bin/env python3
"""
Unit tests for Router v2 (two-stage dispatcher).
Fully offline: all LLM HTTP calls are mocked.
"""

import json
import os
import tempfile
import unittest
import urllib.error
from unittest.mock import MagicMock, patch

from notifybot.actions import ActionDispatcher
from notifybot.cli import load_config
from notifybot.router import LAYER_FALLBACK, LAYER_LLM_ROUTER, LAYER_RULE, LLMRouter, Router


def make_registry():
    """Default registry shape: log, telegram, finance enabled; command disabled."""
    return [
        {"id": "log", "match_hint": "Registrar sem consequência externa.", "type": "builtin", "enabled": True, "config": {}},
        {"id": "telegram", "match_hint": "Alerta pessoal no Telegram.", "type": "builtin", "enabled": True, "config": {}},
        {"id": "finance", "match_hint": "Notificações bancárias e gastos em R$.", "type": "builtin", "enabled": True, "config": {}},
        {"id": "example_command", "match_hint": "Exemplo de comando externo.", "type": "command", "enabled": False, "config": {"command": "/x"}},
    ]


def mock_ollama(content: str):
    """Return a patch of urllib.request.urlopen that answers with given content."""
    mock_response = MagicMock()
    mock_response.read.return_value = json.dumps({"message": {"content": content}}).encode("utf-8")
    # Make `with urlopen(...) as resp` yield the configured response itself.
    mock_response.__enter__.return_value = mock_response
    return patch("urllib.request.urlopen", return_value=mock_response)


NOTIF = {"rec_id": 1, "app": "com.bank", "title": "Compra", "subtitle": "", "body": "R$ 100,00"}
CLASSIFICATION = {"category": "other", "urgency": "medium", "summary": "Compra de R$ 100,00"}


class TestRouterDefaults(unittest.TestCase):
    """load_config defaults must expose Router v2 sections without a catch-all rule."""

    def test_load_config_defaults_phase2(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            missing_path = os.path.join(tmpdir, "missing.json")
            config = load_config(missing_path)
        self.assertIn("actions", config)
        self.assertIn("router", config)
        self.assertIn("finance", config)
        self.assertEqual(config["router"]["min_confidence"], 0.5)
        ids = [a["id"] for a in config["actions"]]
        self.assertIn("log", ids)
        self.assertIn("telegram", ids)
        self.assertIn("finance", ids)
        # finance entry is a working example describing bank/spend notifications
        finance_entry = next(a for a in config["actions"] if a["id"] == "finance")
        self.assertEqual(finance_entry["type"], "builtin")
        self.assertTrue(finance_entry["enabled"])
        self.assertIn("R$", finance_entry["match_hint"])
        # No catch-all rule: every rule has a non-empty condition
        for rule in config["rules"]:
            self.assertTrue(rule.get("condition"), "catch-all rule present in defaults")
        finance_cfg = config["finance"]
        self.assertTrue(finance_cfg["enabled"])
        self.assertEqual(finance_cfg["bank_apps"], [])
        self.assertEqual(finance_cfg["ledger_db"], "finance.db")
        self.assertIn("outros", finance_cfg["categories"])

    def test_load_config_user_file_overrides(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg_path = os.path.join(tmpdir, "config.json")
            with open(cfg_path, "w", encoding="utf-8") as f:
                json.dump({"router": {"min_confidence": 0.8}}, f)
            config = load_config(cfg_path)
        self.assertEqual(config["router"]["min_confidence"], 0.8)
        self.assertIn("actions", config)  # defaults still provide the registry


class TestLLMRouter(unittest.TestCase):
    """Router stage 2: prompt building, tolerant parsing, and safe fallbacks."""

    def setUp(self):
        self.router = LLMRouter(actions_registry=make_registry(), min_confidence=0.5)

    def test_enabled_actions_only(self):
        enabled = self.router.enabled_actions()
        self.assertEqual([a["id"] for a in enabled], ["log", "telegram", "finance"])
        for action in enabled:
            self.assertEqual(sorted(action.keys()), ["id", "match_hint"])

    def test_prompt_only_enabled_actions(self):
        _, user = self.router.build_prompt(NOTIF, CLASSIFICATION)
        self.assertIn('"finance"', user)
        self.assertIn("R$", user)  # match_hint content present
        self.assertIn("Notificação:", user)
        self.assertIn("Categoria:", user)
        self.assertNotIn("example_command", user)  # disabled action never leaks

    def test_route_llm_router_layer(self):
        with mock_ollama('```json\n{"action_id": "finance", "confidence": 0.9, "payload": {"valor": "100,00"}}\n```'):
            decision = self.router.route(NOTIF, CLASSIFICATION)
        self.assertEqual(decision["layer"], LAYER_LLM_ROUTER)
        self.assertEqual(decision["actions"], ["finance"])
        self.assertAlmostEqual(decision["confidence"], 0.9)
        self.assertEqual(decision["payload"], {"valor": "100,00"})
        self.assertIsNone(decision["rule_matched"])

    def test_route_low_confidence_fallback(self):
        with mock_ollama('{"action_id": "finance", "confidence": 0.3, "payload": {}}'):
            decision = self.router.route(NOTIF, CLASSIFICATION)
        self.assertEqual(decision["layer"], LAYER_FALLBACK)
        self.assertEqual(decision["actions"], ["log"])
        self.assertEqual(decision["reason"], "low_confidence")
        self.assertAlmostEqual(decision["confidence"], 0.3)

    def test_route_unknown_action_fallback(self):
        with mock_ollama('{"action_id": "nonexistent", "confidence": 0.9}'):
            decision = self.router.route(NOTIF, CLASSIFICATION)
        self.assertEqual(decision["layer"], LAYER_FALLBACK)
        self.assertEqual(decision["reason"], "unknown_action")

    def test_route_malformed_json_fallback(self):
        with mock_ollama("Isso não é um JSON válido de forma alguma"):
            decision = self.router.route(NOTIF, CLASSIFICATION)
        self.assertEqual(decision["layer"], LAYER_FALLBACK)
        self.assertEqual(decision["reason"], "malformed_response")

    @patch("urllib.request.urlopen", side_effect=urllib.error.URLError("connection refused"))
    def test_route_network_error_fallback(self, _mock_urlopen):
        decision = self.router.route(NOTIF, CLASSIFICATION)
        self.assertEqual(decision["layer"], LAYER_FALLBACK)
        self.assertEqual(decision["reason"], "llm_error")
        self.assertIsNone(decision["confidence"])

    def test_route_empty_registry_fallback(self):
        router = LLMRouter(actions_registry=[], min_confidence=0.5)
        decision = router.route(NOTIF, CLASSIFICATION)
        self.assertEqual(decision["layer"], LAYER_FALLBACK)
        self.assertEqual(decision["reason"], "no_enabled_actions")

    def test_confidence_percent_normalization(self):
        self.assertAlmostEqual(self.router._parse_confidence(80), 0.8)
        self.assertEqual(self.router._parse_confidence(None), 0.0)
        self.assertEqual(self.router._parse_confidence("abc"), 0.0)
        self.assertAlmostEqual(self.router._parse_confidence(1), 1.0)


class TestRouterTwoStage(unittest.TestCase):
    """Composite Router: rules first, LLM router second, safe fallback."""

    def setUp(self):
        self.llm = LLMRouter(actions_registry=make_registry(), min_confidence=0.5)

    def test_rule_layer_overrides_llm(self):
        rules = [{"name": "high_urgency", "condition": {"urgency": ["high"]}, "actions": ["log", "telegram"]}]
        router = Router(rules=rules, llm_router=self.llm)
        decision = router.route(NOTIF, {"category": "alert", "urgency": "high", "summary": "Alerta"})
        self.assertEqual(decision["layer"], LAYER_RULE)
        self.assertEqual(decision["rule_matched"], "high_urgency")
        self.assertEqual(decision["actions"], ["log", "telegram"])

    def test_no_rule_matches_uses_llm(self):
        rules = [{"name": "messages_only", "condition": {"category": ["message"]}, "actions": ["telegram"]}]
        router = Router(rules=rules, llm_router=self.llm)
        with mock_ollama('{"action_id": "finance", "confidence": 0.95, "payload": {}}'):
            decision = router.route(NOTIF, CLASSIFICATION)
        self.assertEqual(decision["layer"], LAYER_LLM_ROUTER)
        self.assertEqual(decision["actions"], ["finance"])

    def test_no_rule_and_llm_down_falls_back_to_log(self):
        router = Router(rules=[], llm_router=self.llm)
        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("down")):
            decision = router.route(NOTIF, CLASSIFICATION)
        self.assertEqual(decision["layer"], LAYER_FALLBACK)
        self.assertEqual(decision["actions"], ["log"])

    def test_no_llm_router_configured_falls_back(self):
        router = Router(rules=[], llm_router=None)
        decision = router.route(NOTIF, CLASSIFICATION)
        self.assertEqual(decision["layer"], LAYER_FALLBACK)
        self.assertEqual(decision["reason"], "no_llm_router")

    def test_llm_router_layer_when_llm_picks_log(self):
        router = Router(rules=[], llm_router=self.llm)
        with mock_ollama('{"action_id": "log", "confidence": 0.8, "payload": {}}'):
            decision = router.route(NOTIF, CLASSIFICATION)
        self.assertEqual(decision["layer"], LAYER_LLM_ROUTER)
        self.assertEqual(decision["actions"], ["log"])


class TestRegistryDrivenDispatcher(unittest.TestCase):
    """ActionDispatcher resolves legacy strings, registry entries and handlers."""

    def dispatch_notif(self, dispatcher, actions):
        notif = {"rec_id": 7, "app": "com.bank", "uuid": "u7", "title": "T", "subtitle": "", "body": "B"}
        classification = {"category": "other", "urgency": "low", "summary": "S"}
        return dispatcher.dispatch(actions, notif, classification, rule_name="none", layer=LAYER_LLM_ROUTER, confidence=0.9)

    def test_registered_handler_recovers_event_metadata(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            log_file = os.path.join(tmpdir, "events.jsonl")
            seen = {}

            def handler(notif, classification, event):
                seen["rec_id"] = event.get("rec_id")
                seen["layer"] = event.get("layer")
                seen["confidence"] = event.get("confidence")
                event["finance"] = {"status": "ok"}
                return "ok"

            dispatcher = ActionDispatcher(log_file=log_file)
            dispatcher.register_action("finance", handler)
            results = self.dispatch_notif(dispatcher, ["finance"])

            self.assertEqual(results["finance"], "ok")
            self.assertEqual(seen["rec_id"], 7)
            self.assertEqual(seen["layer"], LAYER_LLM_ROUTER)
            self.assertAlmostEqual(seen["confidence"], 0.9)
            with open(log_file, "r", encoding="utf-8") as f:
                event = json.loads(f.readline().strip())
            self.assertEqual(event["finance"], {"status": "ok"})
            self.assertEqual(event["action_results"]["finance"], "ok")
            self.assertEqual(event["layer"], LAYER_LLM_ROUTER)
            self.assertEqual(event["router_payload"], {})

    def test_finance_without_handler_is_skipped(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            dispatcher = ActionDispatcher(
                log_file=os.path.join(tmpdir, "events.jsonl"),
                registry=[e for e in make_registry() if e["id"] == "finance"],
            )
            results = self.dispatch_notif(dispatcher, ["finance"])
            self.assertEqual(results["finance"], "skipped_no_handler")

    def test_registry_command_action(self):
        cmd_script = os.path.join(tempfile.gettempdir(), "notifybot_test_cmd.py")
        with open(cmd_script, "w") as f:
            f.write("#!/usr/bin/env python3\nimport sys, json\nd=json.loads(sys.argv[1]); assert d['event']\n")
        os.chmod(cmd_script, 0o755)
        registry = [{
            "id": "example_command", "match_hint": "Exemplo.",
            "type": "command", "enabled": False, "config": {"command": cmd_script},
        }]
        with tempfile.TemporaryDirectory() as tmpdir:
            dispatcher = ActionDispatcher(
                log_file=os.path.join(tmpdir, "events.jsonl"),
                registry=registry,
            )
            results = self.dispatch_notif(dispatcher, ["example_command"])
            self.assertEqual(results["example_command"], "ok")

    def test_unknown_action_skipped(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            dispatcher = ActionDispatcher(log_file=os.path.join(tmpdir, "events.jsonl"))
            results = self.dispatch_notif(dispatcher, ["mystery_action"])
            self.assertEqual(results["mystery_action"], "skipped_unknown_action")
            self.assertEqual(results["log"], "ok")

    def test_handler_exception_is_reported_not_raised(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            dispatcher = ActionDispatcher(log_file=os.path.join(tmpdir, "events.jsonl"))
            dispatcher.register_action("finance", lambda n, c, e: 1 / 0)
            results = self.dispatch_notif(dispatcher, ["finance"])
            self.assertTrue(results["finance"].startswith("error:"))
            self.assertEqual(results["log"], "ok")


class TestRunCycleFinanceWiring(unittest.TestCase):
    """Offline integration: run_cycle routes a bank notification to finance → ledger."""

    def _make_synthetic_db(self, db_file):
        import plistlib
        import sqlite3
        conn = sqlite3.connect(db_file)
        cur = conn.cursor()
        cur.execute("CREATE TABLE app (app_id INTEGER PRIMARY KEY, identifier VARCHAR, badge INTEGER);")
        cur.execute(
            "CREATE TABLE record (rec_id INTEGER PRIMARY KEY, app_id INTEGER, uuid BLOB, "
            "data BLOB, delivered_date REAL, presented Bool);"
        )
        cur.execute("INSERT INTO app VALUES (1, 'com.itau', 0);")
        sample = {
            "app": "com.itau",
            "date": 814583607.0,
            "req": {
                "titl": "Itaú",
                "subt": "banco",
                "body": "Compra aprovada: R$ 123,45 no iFood - Restaurante",
            },
        }
        blob = plistlib.dumps(sample, fmt=plistlib.FMT_BINARY)
        cur.execute("INSERT INTO record VALUES (99, 1, NULL, ?, 814583607.0, 1);", (blob,))
        conn.commit()
        conn.close()

    def test_run_cycle_routes_finance_and_fills_ledger(self):
        import datetime
        import plistlib
        import sqlite3 as sql3
        from unittest.mock import MagicMock

        from notifybot.actions import ActionDispatcher
        from notifybot.cli import build_finance_runtime, run_cycle
        from notifybot.finance import FinanceLedger
        from notifybot.reader import NotificationReader
        from notifybot.router import LLMRouter, Router
        from notifybot.state import StateManager

        with tempfile.TemporaryDirectory() as tmpdir:
            db_file = os.path.join(tmpdir, "db")
            self._make_synthetic_db(db_file)
            state_file = os.path.join(tmpdir, "state.json")
            log_file = os.path.join(tmpdir, "events.jsonl")
            ledger_path = os.path.join(tmpdir, "finance.db")

            config = {
                "state_file": state_file,
                "log_file": log_file,
                "ollama": {"url": "http://127.0.0.1:11434/api/chat", "model": "m", "timeout": 5},
                "router": {"min_confidence": 0.5, "timeout": 5},
                "actions": [
                    {"id": "log", "match_hint": "Registrar.", "type": "builtin", "enabled": True, "config": {}},
                    {"id": "telegram", "match_hint": "Alertas pessoais.", "type": "builtin", "enabled": True, "config": {}},
                    {"id": "finance", "match_hint": "Bancárias e gastos em R$.", "type": "builtin", "enabled": True, "config": {}},
                ],
                "rules": [],
                "finance": {
                    "enabled": True,
                    "ledger_db": ledger_path,
                    "bank_apps": [],
                    "categories": ["alimentacao", "outros"],
                    "telegram_summary": False,
                },
                "telegram": {"token": "", "chat_id": ""},
            }

            dispatcher = ActionDispatcher(
                log_file=log_file, telegram_token="", telegram_chat_id="",
                registry=config["actions"],
            )
            build_finance_runtime(config, dispatcher)
            router = Router(
                rules=[],
                llm_router=LLMRouter(actions_registry=config["actions"], min_confidence=0.5, timeout=5),
            )
            state_mgr = StateManager(state_file=state_file)
            reader = NotificationReader(db_path=db_file)
            classifier = MagicMock()
            classifier.classify.return_value = {
                "category": "other", "urgency": "medium", "summary": "Compra no iFood",
            }

            with mock_ollama('{"action_id": "finance", "confidence": 0.95, "payload": {}}'):
                processed = run_cycle(
                    reader=reader, state_mgr=state_mgr, classifier=classifier,
                    router=router, dispatcher=dispatcher, is_startup=True, mode="once",
                )
            self.assertEqual(processed, 1)

            # Ledger row created with decimal-safe amount
            ledger = FinanceLedger(ledger_path)
            records = ledger.month_records(datetime.datetime.now().astimezone().strftime("%Y-%m"))
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["rec_id"], 99)
            self.assertEqual(str(records[0]["amount"]), "123.45")
            self.assertEqual(records[0]["kind"], "gasto")
            self.assertEqual(records[0]["category"], "alimentacao")
            ledger.close()

            # Event line carries layer + finance evidence
            with open(log_file, "r", encoding="utf-8") as f:
                events = [json.loads(line.strip()) for line in f if line.strip()]
            notif_events = [e for e in events if e.get("event") == "notification"]
            self.assertEqual(len(notif_events), 1)
            event = notif_events[0]
            self.assertEqual(event["layer"], LAYER_LLM_ROUTER)
            self.assertAlmostEqual(event["confidence"], 0.95)
            self.assertEqual(event["finance"]["status"], "ok")
            self.assertEqual(event["finance"]["method"], "regex")
            self.assertEqual(event["finance"]["stored"], "inserted")
            self.assertEqual(event["action_results"]["finance"], "ok")

            # Idempotency: rerunning the same cycle adds no duplicate row
            state_mgr2 = StateManager(state_file=state_file)
            reader2 = NotificationReader(db_path=db_file)
            processed2 = run_cycle(
                reader=reader2, state_mgr=state_mgr2, classifier=classifier,
                router=router, dispatcher=dispatcher, is_startup=False, mode="once",
            )
            self.assertEqual(processed2, 0)



if __name__ == "__main__":
    unittest.main()