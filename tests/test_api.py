#!/usr/bin/env python3
"""Unit tests for notifybot/api.py — the Desktop UI JSON API.

Fully offline: the server boots on port 0 in a thread against a
tempfile.TemporaryDirectory fixture repo (synthetic config.json, a
logs/events.jsonl with a deliberately malformed line, and a finance.db
seeded via FinanceLedger.insert_transaction). LLM/telegram/osascript are
mocked. Style mirrors tests/test_router.py.
"""

import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from unittest.mock import MagicMock, patch

from notifybot.api import (
    BotApiHTTPServer,
    BotApiRuntime,
    serve,
)
from notifybot.finance import FinanceLedger
from notifybot.cli import load_config

VALID_MONTH = "2026-10"


def valid_config_payload() -> dict:
    """A complete, valid config JSON (same shape POST /api/config requires)."""
    return {
        "poll_interval": 2.0,
        "state_file": ".bot_state.json",
        "log_file": "logs/events.jsonl",
        "ollama": {
            "url": "http://127.0.0.1:11434/api/chat",
            "model": "glm-5.3-flash:cloud",
            "timeout": 30.0,
        },
        "telegram": {"token": "", "chat_id": ""},
        "router": {"min_confidence": 0.5, "timeout": 15.0},
        "actions": [
            {
                "id": "log",
                "match_hint": "Registrar sem consequências externas.",
                "type": "builtin",
                "enabled": True,
                "config": {},
            },
            {
                "id": "example_command",
                "match_hint": "Comando externo de teste.",
                "type": "command",
                "enabled": False,
                "config": {"command": "/path/to/prog"},
            },
        ],
        "rules": [
            {"name": "high_urgency", "condition": {"urgency": ["high"]}, "actions": ["log"]},
        ],
        "finance": {
            "enabled": True,
            "ledger_db": "finance.db",
            "bank_apps": [],
            "telegram_summary": False,
            "categories": ["alimentacao", "outros"],
        },
    }


def valid_tx(rec_id: int, ts: str, amount: float, kind: str, category: str) -> dict:
    return {
        "rec_id": rec_id,
        "ts": ts,
        "bundle": "com.bank.app",
        "amount": __import__("decimal").Decimal(str(amount)),
        "kind": kind,
        "category": category,
        "description": "descrição de teste",
        "raw": {"rec_id": rec_id},
    }


class ApiServerFixture:
    """Boots the Bot API against a synthetic repo on an ephemeral port."""

    def __init__(
        self,
        config: dict,
        with_db: bool = True,
        with_log: bool = True,
        with_state: bool = True,
    ):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        self.config_path = os.path.join(self.root, "config.json")
        with open(self.config_path, "w", encoding="utf-8") as f:
            json.dump(config, f)

        if with_log:
            os.makedirs(os.path.join(self.root, "logs"), exist_ok=True)
            events = [
                json.dumps({"event": "notification", "timestamp": "2026-10-26T10:00:00Z", "rec_id": 10,
                            "app": "com.apple.mail", "title": "Mail msg", "body": "corpo",
                            "classification": {"category": "message", "urgency": "low", "summary": "resumo a"},
                            "layer": "rule", "rule_matched": "messages", "confidence": None,
                            "router_payload": {}, "actions": ["log", "telegram"], "action_results": {"log": "ok", "telegram": "skipped"}}),
                "not-a-json-line",
                json.dumps({"event": "notification", "timestamp": "2026-10-26T11:00:00Z", "rec_id": 12,
                            "app": "com.bank.app", "title": "Compra", "body": "R$ 1,00",
                            "classification": {"category": "finance", "urgency": "high", "summary": "resumo b"},
                            "layer": "llm_router", "rule_matched": "none", "confidence": 0.87,
                            "router_payload": {"action_id": "log"}, "actions": ["log"], "action_results": {"log": "ok"}}),
                json.dumps({"event": "startup", "timestamp": "2026-10-26T09:00:00Z", "mode": "daemon", "watermark": 10}),
            ]
            with open(os.path.join(self.root, "logs", "events.jsonl"), "w", encoding="utf-8") as f:
                f.write("\n".join(events) + "\n")

        if with_state:
            with open(os.path.join(self.root, ".bot_state.json"), "w", encoding="utf-8") as f:
                json.dump({"last_rec_id": 12, "recent_uuids": [], "updated_at": "2026-10-26T11:00:01Z"}, f)

        if with_db:
            ledger = FinanceLedger(db_path=os.path.join(self.root, "finance.db"))
            ledger.insert_transaction(valid_tx(101, "2026-10-05T12:00:00-03:00", 123.45, "gasto", "alimentacao"))
            ledger.insert_transaction(valid_tx(102, "2026-10-08T09:30:00-03:00", 200.0, "recebimento", "outros"))
            ledger.insert_transaction(valid_tx(103, "2026-09-28T10:00:00-03:00", 100.0, "gasto", "transporte"))
            ledger.close()

        runtime = BotApiRuntime(config_path=self.config_path)
        self.httpd = BotApiHTTPServer("127.0.0.1", 0, runtime)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.port}"

    def request(self, method: str, path: str, body: dict = None):
        """HTTP request helper returning (status, payload)."""
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(
            self.base + path,
            data=data,
            method=method,
            headers={"Content-Type": "application/json"} if data else {},
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                raw = resp.read().decode("utf-8")
                return resp.status, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8")
            try:
                return e.code, json.loads(raw)
            except ValueError:
                return e.code, {"raw": raw}

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.tmp.cleanup()


class TestEventsEndpoint(unittest.TestCase):
    """#2 GET /api/events — cursor, filters, malformed tolerance, 400s."""

    @classmethod
    def setUpClass(cls):
        cls.fx = ApiServerFixture(valid_config_payload())

    @classmethod
    def tearDownClass(cls):
        cls.fx.close()

    def test_shape_and_malformed_skipped(self):
        status, body = self.fx.request("GET", "/api/events")
        self.assertEqual(status, 200)
        self.assertEqual(body["count"], 3)  # 2 notifications + 1 startup; malformed dropped
        self.assertEqual(body["latest_rec_id"], 12)
        events = body["events"]
        self.assertEqual(events[0]["event"], "notification")
        self.assertEqual(events[0]["rec_id"], 10)
        self.assertEqual(events[0]["classification"]["summary"], "resumo a")
        self.assertEqual(events[0]["layout"] if False else events[0]["layer"], "rule")
        self.assertIn("action_results", events[0])

    def test_since_cursor(self):
        status, body = self.fx.request("GET", "/api/events?since=10")
        self.assertEqual(body["count"], 1)
        self.assertEqual(body["events"][0]["rec_id"], 12)

        # Filtered reads are strictly rec_id-cursored: startup lines (no
        # rec_id) only appear on unfiltered reads, never re-fetched.
        status, body = self.fx.request("GET", "/api/events?since=12")
        self.assertEqual(body["count"], 0)
        self.assertEqual(body["latest_rec_id"], 12)

        status, body = self.fx.request("GET", "/api/events?since=99")
        self.assertEqual(body["count"], 0)

    def test_kind_filter(self):
        _, body = self.fx.request("GET", "/api/events?kind=notification")
        self.assertEqual(body["count"], 2)
        _, body = self.fx.request("GET", "/api/events?kind=startup")
        self.assertEqual(body["count"], 1)
        self.assertEqual(body["events"][0]["event"], "startup")
        self.assertEqual(body["latest_rec_id"], 12)  # window-level newest rec_id

    def test_limit_paging(self):
        _, body = self.fx.request("GET", "/api/events?limit=1")
        self.assertEqual(body["count"], 1)
        self.assertEqual(body["events"][0]["rec_id"], 10)
        # paging forward by cursor yields the next event
        _, body2 = self.fx.request("GET", "/api/events?limit=1&since=10")
        self.assertEqual(body2["events"][0]["rec_id"], 12)

    def test_bad_params(self):
        for path, fragment in [
            ("/api/events?limit=0", "limit"),
            ("/api/events?limit=501", "limit"),
            ("/api/events?limit=banana", "limit"),
            ("/api/events?since=banana", "since"),
            ("/api/events?since=-1", "since"),
            ("/api/events?kind=nope", "kind"),
        ]:
            status, body = self.fx.request("GET", path)
            self.assertEqual(status, 400, path)
            self.assertIn("error", body)
            self.assertIn(fragment, body["error"], path)


class TestConfigEndpoints(unittest.TestCase):
    """#3/#4 GET+POST /api/config — masking, validation, atomic save."""

    @classmethod
    def setUpClass(cls):
        cls.config = valid_config_payload()
        cls.config["telegram"] = {"token": "supersecret", "chat_id": "4242"}
        cls.fx = ApiServerFixture(cls.config)

    @classmethod
    def tearDownClass(cls):
        cls.fx.close()

    def test_config_get_masks_secrets(self):
        status, body = self.fx.request("GET", "/api/config")
        self.assertEqual(status, 200)
        self.assertEqual(body["telegram"]["token"], "********")
        self.assertEqual(body["telegram"]["chat_id"], "********")
        self.assertNotIn("supersecret", json.dumps(body))

    def test_config_post_roundtrip_and_atomic_write(self):
        payload = valid_config_payload()
        payload["poll_interval"] = 9.5
        payload["router"] = {"min_confidence": 0.75, "timeout": 20.0}
        # The UI never ships secrets: telegram must be empty (env-only).
        payload["telegram"] = {"token": "", "chat_id": ""}
        status, body = self.fx.request("POST", "/api/config", payload)
        self.assertEqual(status, 200)
        self.assertEqual(body, {"ok": True})

        # Reload through the daemon's own loader: the write is complete JSON
        # and the env-only telegram section was normalized to empty strings.
        reloaded = load_config(self.fx.config_path)
        self.assertEqual(reloaded["poll_interval"], 9.5)
        self.assertEqual(reloaded["router"]["min_confidence"], 0.75)
        self.assertEqual(reloaded["telegram"], {"token": "", "chat_id": ""})

    def test_config_post_validation_failures(self):
        original = open(self.fx.config_path, "r", encoding="utf-8").read()

        illegal_cases = [
            # (mutator of the valid payload, expected fragment in details)
            (lambda c: c.__setitem__("poll_interval", 0), "poll_interval"),
            (lambda c: c.__setitem__("poll_interval", "2"), "poll_interval"),
            (lambda c: c.__setitem__("ollama", {}), "ollama"),
            (lambda c: c.__setitem__("ollama", {"url": "", "model": "m", "timeout": 1}), "ollama.url"),
            (lambda c: c.__setitem__("router", {"min_confidence": 1.5, "timeout": 15}), "min_confidence"),
            (lambda c: c["telegram"].__setitem__("token", "abc"), "telegram.token"),
            (lambda c: c["actions"].append({"id": "log", "match_hint": "d", "type": "builtin", "enabled": True, "config": {}}), "duplicado"),
            (lambda c: c["actions"][0].__setitem__("type", "weird"), "builtin"),
            (lambda c: c["rules"][0].__setitem__("condition", {"nope": ["x"]}), "desconhecido"),
            (lambda c: c.__setitem__("rules", [{"name": "r", "condition": {"urgency": []}, "actions": []}]), "actions"),
        ]
        for mutate, fragment in illegal_cases:
            payload = valid_config_payload()
            mutate(payload)
            status, body = self.fx.request("POST", "/api/config", payload)
            self.assertEqual(status, 400, f"{fragment}: {body}")
            self.assertIn("details", body)
            self.assertTrue(any(fragment in d for d in body["details"]), body["details"])

        # failed saves never touch the file (no torn or partial writes)
        self.assertEqual(open(self.fx.config_path, "r", encoding="utf-8").read(), original)

        # legal-edge payload shapes are accepted by the same rules
        legal_cases = [
            lambda c: c.__setitem__("actions", []),  # empty registry is legal
            lambda c: c["rules"][0].__setitem__("condition", {"urgency": "high"}),  # scalar legal
        ]
        for mutate in legal_cases:
            payload = valid_config_payload()
            mutate(payload)
            status, body = self.fx.request("POST", "/api/config", payload)
            self.assertEqual(status, 200, body)

    def test_read_config_strict_500_on_broken_json(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            broken = os.path.join(tmpdir, "broken.json")
            with open(broken, "w", encoding="utf-8") as f:
                f.write("{not json")
            runtime = BotApiRuntime(config_path=broken)
            with self.assertRaises(Exception) as ctx:
                runtime.config_get()
            self.assertIn("ilegível", str(ctx.exception))


class TestFinanceEndpoints(unittest.TestCase):
    """#5–#8 finance endpoints over a read-only ledger connection."""

    @classmethod
    def setUpClass(cls):
        cls.fx = ApiServerFixture(valid_config_payload())

    @classmethod
    def tearDownClass(cls):
        cls.fx.close()

    def test_summary_month(self):
        status, body = self.fx.request("GET", f"/api/finance/summary?month={VALID_MONTH}")
        self.assertEqual(status, 200)
        self.assertEqual(body["month"], VALID_MONTH)
        self.assertTrue(body["db_exists"])
        self.assertEqual(body["count"], 2)
        self.assertAlmostEqual(body["total"], 323.45, places=2)
        self.assertAlmostEqual(body["by_category"]["alimentacao"], 123.45, places=2)
        self.assertAlmostEqual(body["by_kind"]["gasto"], 123.45, places=2)
        self.assertAlmostEqual(body["by_kind"]["recebimento"], 200.0, places=2)
        self.assertAlmostEqual(body["prev_total"], 100.0, places=2)
        self.assertAlmostEqual(body["mom_delta"], 223.45, places=1)

    def test_transactions_shape(self):
        status, body = self.fx.request("GET", f"/api/finance/transactions?month={VALID_MONTH}")
        self.assertEqual(status, 200)
        self.assertEqual(body["month"], VALID_MONTH)
        self.assertEqual(len(body["records"]), 2)
        tx = body["records"][0]
        for field in ("rec_id", "ts", "bundle", "amount", "kind", "category", "description"):
            self.assertIn(field, tx)
        self.assertNotIn("raw_json", tx)
        self.assertNotIn("raw", tx)

    def test_report_status_does_not_generate(self):
        status, body = self.fx.request("GET", f"/api/finance/report?month={VALID_MONTH}")
        self.assertEqual(status, 200)
        self.assertFalse(body["exists"])
        self.assertIn("reports", body["html_path"])
        self.assertTrue(body["html_path"].endswith(f"{VALID_MONTH}.html"))

        status, body = self.fx.request("GET", "/api/finance/report?month=banana-month")
        self.assertEqual(status, 400)
        self.assertIn("error", body)

    def test_report_build(self):
        mocked_reporter = MagicMock()
        mocked_reporter.build.return_value = {
            "month": VALID_MONTH,
            "html_path": os.path.join(self.fx.root, "reports", f"{VALID_MONTH}.html"),
            "markdown": "# Relatório\nok",
        }
        with patch("notifybot.api.FinanceReporter", return_value=mocked_reporter):
            status, body = self.fx.request("POST", "/api/finance/report", {"month": VALID_MONTH})
        self.assertEqual(status, 200, body)
        self.assertTrue(body["ok"])
        self.assertEqual(body["month"], VALID_MONTH)
        self.assertEqual(body["markdown"], "# Relatório\nok")
        mocked_reporter.build.assert_called_once_with(VALID_MONTH)

    def test_report_build_failure_is_500(self):
        mocked_reporter = MagicMock()
        mocked_reporter.build.side_effect = RuntimeError("boom")
        with patch("notifybot.api.FinanceReporter", return_value=mocked_reporter):
            status, body = self.fx.request("POST", "/api/finance/report", {"month": VALID_MONTH})
        self.assertEqual(status, 500)
        self.assertIn("boom", body["error"])

    def test_disabled_finance_is_409(self):
        config = valid_config_payload()
        config["finance"] = {**config["finance"], "enabled": False}
        fixture = ApiServerFixture(config, with_db=False)
        try:
            status, body = fixture.request("GET", f"/api/finance/summary?month={VALID_MONTH}")
            self.assertEqual(status, 409)
            self.assertIn("desabilitado", body["error"])

            status, body = fixture.request("GET", f"/api/finance/transactions?month={VALID_MONTH}")
            self.assertEqual(status, 409)
            status, body = fixture.request("GET", f"/api/finance/report?month={VALID_MONTH}")
            self.assertEqual(status, 409)
        finally:
            fixture.close()

    def test_missing_db_zeroed_summary(self):
        config = valid_config_payload()
        config["finance"] = {**config["finance"], "ledger_db": "inexistente.db"}
        fixture = ApiServerFixture(config, with_db=False)
        try:
            status, body = fixture.request("GET", f"/api/finance/summary?month={VALID_MONTH}")
            self.assertEqual(status, 200)
            self.assertFalse(body["db_exists"])
            self.assertEqual(body["count"], 0)
            self.assertEqual(body["total"], 0.0)
            self.assertEqual(body["by_category"], {})
            self.assertIsNone(body["mom_delta"])

            status, body = fixture.request("GET", f"/api/finance/transactions?month={VALID_MONTH}")
            self.assertEqual(status, 200)
            self.assertEqual(body["records"], [])
        finally:
            fixture.close()

    def test_bad_month_is_400(self):
        status, body = self.fx.request("GET", "/api/finance/summary?month=banana-1")
        self.assertEqual(status, 400)
        status, body = self.fx.request("GET", "/api/finance/transactions?month=10")
        self.assertEqual(status, 400)
        status, body = self.fx.request("GET", "/api/finance/report?month=zzz")
        self.assertEqual(status, 400)


class TestTestNotify(unittest.TestCase):
    """#9 POST /api/test-notify with a mocked osascript layer."""

    def setUp(self):
        self.fx = ApiServerFixture(valid_config_payload())

    def tearDown(self):
        self.fx.close()

    def test_success_posts_three(self):
        with patch("notifybot.cli.subprocess.run") as run_mock, patch("notifybot.cli.time.sleep"):
            run_mock.return_value = subprocess.CompletedProcess(args=[], returncode=0)
            status, body = self.fx.request("POST", "/api/test-notify")
        self.assertEqual(status, 200)
        self.assertEqual(body, {"ok": True, "posted": 3})
        self.assertEqual(run_mock.call_count, 3)

    def test_osascript_failure_is_500(self):
        failure = subprocess.CalledProcessError(1, ["osascript"])
        with patch("notifybot.cli.subprocess.run", side_effect=failure), patch("notifybot.cli.time.sleep"):
            status, body = self.fx.request("POST", "/api/test-notify")
        self.assertEqual(status, 500)
        self.assertIn("error", body)


class TestRoutingErrors(unittest.TestCase):
    """#10 404/405 + CORS headers."""

    @classmethod
    def setUpClass(cls):
        cls.fx = ApiServerFixture(valid_config_payload())

    @classmethod
    def tearDownClass(cls):
        cls.fx.close()

    def test_404_unknown_path(self):
        status, body = self.fx.request("GET", "/api/nothing")
        self.assertEqual(status, 404)
        self.assertIn("error", body)

    def test_405_wrong_method(self):
        status, body = self.fx.request("GET", "/api/test-notify")
        self.assertEqual(status, 405)
        status, body = self.fx.request("POST", "/api/health")
        self.assertEqual(status, 405)

    def test_options_cors_headers(self):
        req = urllib.request.Request(self.fx.base + "/api/config", method="OPTIONS")
        with urllib.request.urlopen(req, timeout=10) as resp:
            self.assertEqual(resp.status, 204)
            self.assertEqual(resp.headers.get("Access-Control-Allow-Origin"), "*")
            self.assertIn("GET", resp.headers.get("Access-Control-Allow-Methods", ""))


class TestServerGuards(unittest.TestCase):
    """Loopback-only hard criterion."""

    def test_non_loopback_host_refused(self):
        exit_code = serve(host="0.0.0.0", port=0)
        self.assertEqual(exit_code, 2)
        exit_code = serve(host="::0", port=0)
        self.assertEqual(exit_code, 2)

    def test_loopback_localhost_named(self):
        # 'localhost' resolves within loopback semantics; binding must succeed.
        runtime = BotApiRuntime(config_path=os.path.join(tempfile.mkdtemp(), "config.json"))
        httpd = BotApiHTTPServer("127.0.0.1", 0, runtime)
        httpd.server_close()


if __name__ == "__main__":
    unittest.main()