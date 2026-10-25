#!/usr/bin/env python3
"""
Unit test suite for macOS Notification Bot.
Fully offline (mocked network for Ollama and Telegram).
Tests run under Python 3.9 standard library with `python3 -m unittest discover -s tests -v`.
"""

import io
import json
import os
import plistlib
import shutil
import sqlite3
import tempfile
import unittest
import urllib.error
from unittest.mock import MagicMock, patch
import uuid

import bot


class TestPlistParserAndReader(unittest.TestCase):
    """Tests binary plist decoding, CoreData timestamps, and SQLite snapshotting."""

    def test_parse_plist_blob_valid(self):
        sample = {
            "app": "com.apple.MobileSMS",
            "date": 814583607.0,
            "req": {
                "titl": "Alice",
                "subt": "iMessage",
                "body": "Lunch?",
            },
        }
        blob = plistlib.dumps(sample, fmt=plistlib.FMT_BINARY)
        parsed = bot.parse_plist_blob(blob)
        self.assertEqual(parsed["app"], "com.apple.MobileSMS")
        self.assertEqual(parsed["req"]["titl"], "Alice")
        self.assertEqual(parsed["req"]["body"], "Lunch?")

    def test_parse_plist_blob_corrupt_or_empty(self):
        self.assertEqual(bot.parse_plist_blob(None), {})
        self.assertEqual(bot.parse_plist_blob(b""), {})
        self.assertEqual(bot.parse_plist_blob(b"not a valid plist binary"), {})

    def test_coredata_to_iso(self):
        # 814583607.0 seconds after 2001-01-01 00:00:00 UTC
        iso_str = bot.coredata_to_iso(814583607.0)
        self.assertTrue(iso_str.startswith("2026-10-25T01:13:27"))
        # Missing or invalid date should return empty string without error
        self.assertEqual(bot.coredata_to_iso(None), "")
        self.assertEqual(bot.coredata_to_iso("invalid"), "")

    def test_format_uuid(self):
        raw_uuid = uuid.uuid4().bytes
        expected = str(uuid.UUID(bytes=raw_uuid))
        self.assertEqual(bot.format_uuid(raw_uuid), expected)
        self.assertEqual(bot.format_uuid(None), "")
        self.assertEqual(bot.format_uuid(b"short"), "73686f7274")

    def test_snapshot_and_read_synthetic_db(self):
        """Creates a synthetic SQLite DB with app and record tables, snapshots, and reads."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_file = os.path.join(tmpdir, "db")
            conn = sqlite3.connect(db_file)
            cur = conn.cursor()
            cur.execute("CREATE TABLE app (app_id INTEGER PRIMARY KEY, identifier VARCHAR, badge INTEGER);")
            cur.execute("CREATE TABLE record (rec_id INTEGER PRIMARY KEY, app_id INTEGER, uuid BLOB, data BLOB, delivered_date REAL, presented Bool);")

            cur.execute("INSERT INTO app VALUES (1, 'com.example.testapp', 0);")

            raw_uuid = uuid.uuid4().bytes
            sample_data = {
                "app": "com.example.testapp",
                "date": 814583607.0,
                "req": {
                    "titl": "Test Title",
                    "subt": "Test Subtitle",
                    "body": "Test message body",
                },
            }
            blob = plistlib.dumps(sample_data, fmt=plistlib.FMT_BINARY)
            cur.execute("INSERT INTO record VALUES (10, 1, ?, ?, 814583607.0, 1);", (raw_uuid, blob))
            conn.commit()
            conn.close()

            # Create empty wal and shm files
            open(f"{db_file}-wal", "w").close()
            open(f"{db_file}-shm", "w").close()

            reader = bot.NotificationReader(db_path=db_file)
            notifs = reader.fetch_notifications(since_rec_id=0)

            self.assertEqual(len(notifs), 1)
            n = notifs[0]
            self.assertEqual(n["rec_id"], 10)
            self.assertEqual(n["app"], "com.example.testapp")
            self.assertEqual(n["title"], "Test Title")
            self.assertEqual(n["subtitle"], "Test Subtitle")
            self.assertEqual(n["body"], "Test message body")
            self.assertEqual(n["uuid"], str(uuid.UUID(bytes=raw_uuid)))

            # Test app cache
            self.assertIn(1, reader.app_cache)
            self.assertEqual(reader.app_cache[1], "com.example.testapp")

    def test_app_cache_prefilled_from_table_and_blob_missing_app(self):
        """Covers the prefill path where the blob lacks an `app` key, served from the app table."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_file = os.path.join(tmpdir, "db")
            conn = sqlite3.connect(db_file)
            cur = conn.cursor()
            cur.execute("CREATE TABLE app (app_id INTEGER PRIMARY KEY, identifier VARCHAR, badge INTEGER);")
            cur.execute("CREATE TABLE record (rec_id INTEGER PRIMARY KEY, app_id INTEGER, uuid BLOB, data BLOB, delivered_date REAL, presented Bool);")

            cur.execute("INSERT INTO app VALUES (1, 'com.example.tableapp', 0);")

            raw_uuid = uuid.uuid4().bytes
            sample_data = {
                "date": 814583607.0,
                "req": {
                    "titl": "Table App",
                    "subt": "",
                    "body": "no app key here",
                },
            }
            blob = plistlib.dumps(sample_data, fmt=plistlib.FMT_BINARY)
            cur.execute("INSERT INTO record VALUES (10, 1, ?, ?, 814583607.0, 1);", (raw_uuid, blob))
            conn.commit()
            conn.close()

            reader = bot.NotificationReader(db_path=db_file)
            notifs = reader.fetch_notifications(since_rec_id=0)

            self.assertEqual(len(notifs), 1)
            n = notifs[0]
            self.assertEqual(n["app"], "com.example.tableapp")
            self.assertEqual(n["title"], "Table App")

            # Cache reflects a warm prefill from the snapshot's app table.
            self.assertIn(1, reader.app_cache)
            self.assertEqual(reader.app_cache[1], "com.example.tableapp")

            # Second fetch exercises the warm-cache fast path.
            again = reader.fetch_notifications(since_rec_id=0)
            self.assertEqual(len(again), 1)
            self.assertEqual(again[0]["app"], "com.example.tableapp")

    def test_missing_plist_keys_graceful_handling(self):
        """Ensures notifications with missing req keys don't trigger KeyErrors."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_file = os.path.join(tmpdir, "db")
            conn = sqlite3.connect(db_file)
            cur = conn.cursor()
            cur.execute("CREATE TABLE app (app_id INTEGER PRIMARY KEY, identifier VARCHAR, badge INTEGER);")
            cur.execute("CREATE TABLE record (rec_id INTEGER PRIMARY KEY, app_id INTEGER, uuid BLOB, data BLOB, delivered_date REAL, presented Bool);")
            # Plist without "req" dictionary
            sample_data = {"app": "com.example.bare"}
            blob = plistlib.dumps(sample_data, fmt=plistlib.FMT_BINARY)
            cur.execute("INSERT INTO record VALUES (1, 1, NULL, ?, NULL, 0);", (blob,))
            conn.commit()
            conn.close()

            reader = bot.NotificationReader(db_path=db_file)
            notifs = reader.fetch_notifications(since_rec_id=0)
            self.assertEqual(len(notifs), 1)
            self.assertEqual(notifs[0]["title"], "")
            self.assertEqual(notifs[0]["subtitle"], "")
            self.assertEqual(notifs[0]["body"], "")


class TestStateManager(unittest.TestCase):
    """Tests watermark tracking, UUID duplicate suppression, and atomic file saving."""

    def test_watermark_and_dedupe(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = os.path.join(tmpdir, "state.json")
            mgr = bot.StateManager(state_file=state_file, max_recent_uuids=5)

            self.assertEqual(mgr.last_rec_id, 0)
            self.assertFalse(mgr.is_duplicate(1, "uuid-1"))

            mgr.record_seen(1, "uuid-1")
            self.assertEqual(mgr.last_rec_id, 1)
            self.assertTrue(mgr.is_duplicate(1, "uuid-1"))

            # Same UUID on a higher rec_id (presentation event)
            self.assertTrue(mgr.is_duplicate(2, "uuid-1"))

            # Higher rec_id with new UUID is not duplicate
            self.assertFalse(mgr.is_duplicate(2, "uuid-2"))
            mgr.record_seen(2, "uuid-2")
            self.assertEqual(mgr.last_rec_id, 2)

            # Persist and reload
            mgr.save()
            mgr2 = bot.StateManager(state_file=state_file, max_recent_uuids=5)
            self.assertEqual(mgr2.last_rec_id, 2)
            self.assertTrue(mgr2.is_duplicate(2, "uuid-2"))
            self.assertTrue(mgr2.is_duplicate(3, "uuid-1"))

    def test_uuid_cache_capping(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = os.path.join(tmpdir, "state.json")
            mgr = bot.StateManager(state_file=state_file, max_recent_uuids=3)
            mgr.record_seen(1, "u1")
            mgr.record_seen(2, "u2")
            mgr.record_seen(3, "u3")
            mgr.record_seen(4, "u4")

            self.assertEqual(len(mgr.recent_uuids), 3)
            self.assertNotIn("u1", mgr.recent_uuids)
            self.assertIn("u4", mgr.recent_uuids)


class TestLLMClassifier(unittest.TestCase):
    """Tests Ollama client with stubbed HTTP handler and tolerant JSON parsing."""

    def test_clean_json_text(self):
        # Normal JSON
        self.assertEqual(bot.LLMClassifier.clean_json_text('{"a": 1}'), '{"a": 1}')
        # Markdown fenced code
        raw = '```json\n{"category": "message", "urgency": "medium", "summary": "Hi"}\n```'
        self.assertEqual(
            bot.LLMClassifier.clean_json_text(raw),
            '{"category": "message", "urgency": "medium", "summary": "Hi"}',
        )
        # Extra text before and after
        raw2 = 'Sure, here is the JSON:\n{"category": "alert", "urgency": "high", "summary": "Bank"}\nHope this helps!'
        self.assertEqual(
            bot.LLMClassifier.clean_json_text(raw2),
            '{"category": "alert", "urgency": "high", "summary": "Bank"}',
        )

    @patch("urllib.request.urlopen")
    def test_classification_fixture_1_message(self, mock_urlopen):
        """Acceptance test: LLM classification produces valid JSON for Fixture 1 (message)."""
        fixture_1 = {
            "app": "com.apple.MobileSMS",
            "title": "Alice",
            "subtitle": "iMessage",
            "body": "Hey, are you free for lunch today at 1pm?",
        }
        stub_content = '{"category": "message", "urgency": "medium", "summary": "Alice asks if you are free for lunch at 1pm"}'
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({"message": {"content": stub_content}}).encode("utf-8")
        mock_urlopen.return_value.__enter__.return_value = mock_response

        classifier = bot.LLMClassifier()
        res = classifier.classify(fixture_1)

        self.assertEqual(res["category"], "message")
        self.assertEqual(res["urgency"], "medium")
        self.assertEqual(res["summary"], "Alice asks if you are free for lunch at 1pm")

    @patch("urllib.request.urlopen")
    def test_classification_fixture_2_alert(self, mock_urlopen):
        """Acceptance test: LLM classification produces valid JSON for Fixture 2 (alert with markdown fences)."""
        fixture_2 = {
            "app": "com.apple.ScriptEditor2",
            "title": "Bank Security Alert",
            "subtitle": "Immediate Action Required",
            "body": "Suspicious charge of $450 detected on card ending 1234.",
        }
        stub_content = '```json\n{"category": "alert", "urgency": "high", "summary": "Suspicious bank charge of $450 detected"}\n```'
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({"message": {"content": stub_content}}).encode("utf-8")
        mock_urlopen.return_value.__enter__.return_value = mock_response

        classifier = bot.LLMClassifier()
        res = classifier.classify(fixture_2)

        self.assertEqual(res["category"], "alert")
        self.assertEqual(res["urgency"], "high")
        self.assertEqual(res["summary"], "Suspicious bank charge of $450 detected")

    @patch("urllib.request.urlopen")
    def test_classification_fallback_on_network_or_parse_error(self, mock_urlopen):
        """Ensures classifier falls back to category 'other' and urgency 'low' on network failure."""
        mock_urlopen.side_effect = urllib.error.URLError("Connection refused")

        notif = {
            "app": "com.example.app",
            "title": "Server Warning",
            "subtitle": "",
            "body": "High memory usage",
        }
        classifier = bot.LLMClassifier()
        res = classifier.classify(notif)

        self.assertEqual(res["category"], "other")
        self.assertEqual(res["urgency"], "low")
        self.assertEqual(res["summary"], "Server Warning")


class TestRuleRouter(unittest.TestCase):
    """Tests top-down rule evaluation, condition matching, and default fallthrough."""

    def setUp(self):
        self.rules = [
            {
                "name": "high_urgency",
                "condition": {"urgency": ["high"]},
                "actions": ["log", "telegram"],
            },
            {
                "name": "messages",
                "condition": {"category": ["message"]},
                "actions": ["log", "telegram"],
            },
            {
                "name": "promo_or_system",
                "condition": {"category": ["promo", "system"]},
                "actions": ["log"],
            },
            {
                "name": "default",
                "condition": {},
                "actions": ["log"],
            },
        ]
        self.router = bot.RuleRouter(rules=self.rules)

    def test_high_urgency_precedence(self):
        notif = {"app": "com.bank"}
        classification = {"category": "system", "urgency": "high", "summary": "Alert"}
        rule, actions = self.router.evaluate(notif, classification)
        self.assertEqual(rule, "high_urgency")
        self.assertEqual(actions, ["log", "telegram"])

    def test_message_category_match(self):
        notif = {"app": "com.apple.MobileSMS"}
        classification = {"category": "message", "urgency": "medium", "summary": "Hello"}
        rule, actions = self.router.evaluate(notif, classification)
        self.assertEqual(rule, "messages")
        self.assertEqual(actions, ["log", "telegram"])

    def test_system_category_match(self):
        notif = {"app": "com.apple.preferences"}
        classification = {"category": "system", "urgency": "low", "summary": "System update"}
        rule, actions = self.router.evaluate(notif, classification)
        self.assertEqual(rule, "promo_or_system")
        self.assertEqual(actions, ["log"])

    def test_fallthrough(self):
        router_no_default = bot.RuleRouter(rules=[
            {"name": "only_high", "condition": {"urgency": "high"}, "actions": ["telegram"]}
        ])
        rule, actions = router_no_default.evaluate({}, {"urgency": "low", "category": "misc"})
        self.assertEqual(rule, "default_fallthrough")
        self.assertEqual(actions, ["log"])


class TestActionDispatcherAndTelegram(unittest.TestCase):
    """Tests Telegram configuration resolution, missing-token skipping, and action dispatching."""

    def test_resolve_telegram_config(self):
        # 1. Config takes precedence
        cfg = {"token": "cfg-tok", "chat_id": "cfg-chat"}
        tok, chat = bot.resolve_telegram_config(cfg)
        self.assertEqual(tok, "cfg-tok")
        self.assertEqual(chat, "cfg-chat")

        # 2. TELEGRAM_BOT_TOKEN and TELEGRAM_ALLOWED_USERS (first id)
        with patch.dict(os.environ, {
            "TELEGRAM_BOT_TOKEN": "env-tok",
            "TELEGRAM_ALLOWED_USERS": "1111,2222,3333",
        }, clear=True):
            tok, chat = bot.resolve_telegram_config({})
            self.assertEqual(tok, "env-tok")
            self.assertEqual(chat, "1111")

        # 3. Legacy aliases TG_BOT_TOKEN and TG_CHAT_ID
        with patch.dict(os.environ, {
            "TG_BOT_TOKEN": "legacy-tok",
            "TG_CHAT_ID": "legacy-chat",
        }, clear=True):
            tok, chat = bot.resolve_telegram_config({})
            self.assertEqual(tok, "legacy-tok")
            self.assertEqual(chat, "legacy-chat")

    def test_telegram_skipped_when_credentials_missing(self):
        """Correction requirement: missing token => telegram logs SKIPPED, never an error."""
        with tempfile.TemporaryDirectory() as tmpdir:
            log_file = os.path.join(tmpdir, "events.jsonl")
            dispatcher = bot.ActionDispatcher(
                log_file=log_file,
                telegram_token="",
                telegram_chat_id="",
            )

            notif = {
                "rec_id": 1,
                "app": "com.apple.MobileSMS",
                "uuid": "u1",
                "title": "Hi",
                "body": "Hello",
            }
            classification = {"category": "message", "urgency": "medium", "summary": "Hi"}

            results = dispatcher.dispatch(["log", "telegram"], notif, classification, "messages")
            self.assertEqual(results["telegram"], "skipped")
            self.assertEqual(results["log"], "ok")

            with open(log_file, "r", encoding="utf-8") as f:
                line = json.loads(f.readline().strip())
                self.assertEqual(line["telegram"], "skipped")
                self.assertEqual(line["action_results"]["telegram"], "skipped")

    @patch("urllib.request.urlopen")
    def test_telegram_send_success(self, mock_urlopen):
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({"ok": True}).encode("utf-8")
        mock_urlopen.return_value.__enter__.return_value = mock_response

        with tempfile.TemporaryDirectory() as tmpdir:
            log_file = os.path.join(tmpdir, "events.jsonl")
            dispatcher = bot.ActionDispatcher(
                log_file=log_file,
                telegram_token="valid_token",
                telegram_chat_id="12345",
            )
            notif = {"app": "com.bank", "title": "Charge", "body": "$100", "uuid": "u1", "rec_id": 5}
            classification = {"category": "alert", "urgency": "high", "summary": "Charged $100"}

            results = dispatcher.dispatch(["log", "telegram"], notif, classification, "high_urgency")
            self.assertEqual(results["telegram"], "ok")

            # Check that urllib.request.Request was called with Telegram Bot API URL
            self.assertTrue(mock_urlopen.called)
            req = mock_urlopen.call_args[0][0]
            self.assertIn("https://api.telegram.org/botvalid_token/sendMessage", req.full_url)

    def test_command_action_execution(self):
        """Tests command:<path> execution with event JSON passed in argv[1]."""
        with tempfile.TemporaryDirectory() as tmpdir:
            log_file = os.path.join(tmpdir, "events.jsonl")
            # Create a simple python script as command target
            cmd_script = os.path.join(tmpdir, "handler.py")
            with open(cmd_script, "w") as f:
                f.write("#!/usr/bin/env python3\nimport sys, json\nassert len(sys.argv) > 1\ndata = json.loads(sys.argv[1])\nassert data['event'] == 'notification'\n")
            os.chmod(cmd_script, 0o755)

            dispatcher = bot.ActionDispatcher(log_file=log_file)
            notif = {"rec_id": 1, "app": "test", "uuid": "u", "title": "T", "body": "B"}
            classification = {"category": "other", "urgency": "low", "summary": "S"}

            results = dispatcher.dispatch([f"command:{cmd_script}", "log"], notif, classification, "default")
            self.assertEqual(results[f"command:{cmd_script}"], "ok")


if __name__ == "__main__":
    unittest.main()
