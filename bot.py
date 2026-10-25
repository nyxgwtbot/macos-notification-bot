#!/usr/bin/env python3
"""
macOS Notification Bot MVP
Reads macOS notifications from Notification Center SQLite database (db2),
classifies them using local Ollama, and routes actions (log, Telegram, commands).
"""

import argparse
import datetime
import json
import logging
import os
import plistlib
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from typing import Any, Dict, List, Optional, Tuple

COREDATA_EPOCH_OFFSET = 978307200  # seconds between 1970-01-01 and 2001-01-01
COREDATA_EPOCH = datetime.datetime(2001, 1, 1, tzinfo=datetime.timezone.utc)

logger = logging.getLogger("notification_bot")


def get_default_db_path() -> str:
    """Resolve Notification Center db2 path using getconf DARWIN_USER_DIR."""
    try:
        darwin_user_dir = subprocess.check_output(
            ["getconf", "DARWIN_USER_DIR"], text=True
        ).strip()
        # On macOS 12 DARWIN_USER_DIR has a trailing slash
        return os.path.join(darwin_user_dir, "com.apple.notificationcenter/db2/db")
    except Exception as e:
        logger.error("Failed to resolve DARWIN_USER_DIR: %s", e)
        return ""


def parse_plist_blob(blob: Optional[bytes]) -> dict:
    """Safely decode binary plist data. Returns empty dict on any failure."""
    if not blob:
        return {}
    try:
        data = plistlib.loads(blob, fmt=plistlib.FMT_BINARY)
        if isinstance(data, dict):
            return data
        return {}
    except Exception:
        try:
            data = plistlib.loads(blob)
            if isinstance(data, dict):
                return data
            return {}
        except Exception:
            return {}


def format_uuid(val: Any) -> str:
    """Convert binary UUID to string representation."""
    if not val:
        return ""
    if isinstance(val, bytes):
        if len(val) == 16:
            try:
                return str(uuid.UUID(bytes=val))
            except Exception:
                return val.hex()
        return val.hex()
    return str(val)


def coredata_to_iso(date_val: Optional[float]) -> str:
    """Convert Apple CoreData epoch timestamp (seconds since 2001-01-01) to ISO-8601 UTC string."""
    if date_val is None:
        return ""
    try:
        if isinstance(date_val, (int, float)):
            dt = COREDATA_EPOCH + datetime.timedelta(seconds=float(date_val))
            return dt.isoformat()
    except Exception:
        pass
    return ""


def resolve_telegram_config(config_tg: Optional[dict] = None) -> Tuple[str, str]:
    """
    Resolve Telegram token and chat_id following correction rules:
    - Token: config['telegram']['token'] overrides TELEGRAM_BOT_TOKEN and TG_BOT_TOKEN.
    - Chat ID: config['telegram']['chat_id'] overrides TELEGRAM_ALLOWED_USERS (first id) and TG_CHAT_ID.
    """
    config_tg = config_tg or {}
    token = (
        config_tg.get("token")
        or os.environ.get("TELEGRAM_BOT_TOKEN")
        or os.environ.get("TG_BOT_TOKEN")
        or ""
    )

    chat_id = config_tg.get("chat_id")
    if not chat_id:
        allowed = os.environ.get("TELEGRAM_ALLOWED_USERS", "").strip()
        if allowed:
            parts = [p.strip() for p in allowed.split(",") if p.strip()]
            if parts:
                chat_id = parts[0]
    if not chat_id:
        chat_id = os.environ.get("TG_CHAT_ID", "").strip()

    return str(token).strip(), str(chat_id or "").strip()


class NotificationReader:
    """Reads notifications by snapshotting Notification Center SQLite db2 and its WAL files."""

    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or get_default_db_path()
        self.app_cache: Dict[int, str] = {}

    def get_app_identifier(self, conn: sqlite3.Connection, app_id: int) -> str:
        """Cached lookup of bundle identifier by app_id from the app table."""
        if app_id in self.app_cache:
            return self.app_cache[app_id]
        identifier = ""
        try:
            cur = conn.cursor()
            cur.execute("SELECT identifier FROM app WHERE app_id = ?", (app_id,))
            row = cur.fetchone()
            if row and row[0]:
                identifier = str(row[0])
        except Exception as e:
            logger.debug("Failed to lookup app_id %s: %s", app_id, e)
        self.app_cache[app_id] = identifier
        return identifier

    def snapshot_and_read(self, query_fn):
        """
        Creates a temporary copy of db, db-wal, and db-shm, then opens with sqlite3 in read-only URI mode.
        Retries once if a SQLite OperationalError occurs (e.g. transient copy race during active WAL write).
        """
        if not self.db_path or not os.path.exists(self.db_path):
            raise FileNotFoundError(f"Notification database not found at {self.db_path}")

        db_dir = os.path.dirname(self.db_path)
        db_base = os.path.basename(self.db_path)

        def _do_read():
            with tempfile.TemporaryDirectory() as tmpdir:
                for suffix in ["", "-wal", "-shm"]:
                    src_file = os.path.join(db_dir, f"{db_base}{suffix}")
                    if os.path.exists(src_file):
                        shutil.copy2(src_file, tmpdir)
                tmp_db = os.path.join(tmpdir, db_base)
                conn = sqlite3.connect(f"file:{tmp_db}?mode=ro", uri=True)
                try:
                    return query_fn(conn)
                finally:
                    conn.close()

        try:
            return _do_read()
        except sqlite3.OperationalError as e:
            logger.warning("SQLite error on first snapshot read (%s); retrying copy once...", e)
            time.sleep(0.1)
            return _do_read()

    def fetch_notifications(self, since_rec_id: int = 0, limit: Optional[int] = None, reverse: bool = False) -> List[dict]:
        """
        Fetch notifications with rec_id > since_rec_id.
        If reverse is True, orders DESC (e.g. for --dump). Default is ASC (for daemon chronological processing).
        """
        def _query(conn: sqlite3.Connection) -> List[dict]:
            # Prefill app_cache from the snapshot's app table so bundle-id
            # resolution has a warm entry for every known app_id, even when
            # the record blob already embeds an `app` value.
            try:
                prefill = conn.cursor()
                prefill.execute("SELECT app_id, identifier FROM app")
                for row_app_id, row_identifier in prefill:
                    self.app_cache[row_app_id] = row_identifier or ""
            except Exception as e:
                logger.debug("Failed to prefill app cache from app table: %s", e)

            order = "DESC" if reverse else "ASC"
            sql = f"SELECT rec_id, app_id, uuid, data, delivered_date, presented FROM record WHERE rec_id > ? ORDER BY rec_id {order}"
            params: list = [since_rec_id]
            if limit:
                sql += f" LIMIT {int(limit)}"

            cur = conn.cursor()
            cur.execute(sql, params)
            rows = cur.fetchall()

            results = []
            for row in rows:
                rec_id, app_id, raw_uuid, data_blob, delivered_date, presented = row
                data = parse_plist_blob(data_blob)
                req = data.get("req") if isinstance(data.get("req"), dict) else {}

                app_bundle = data.get("app") or self.get_app_identifier(conn, app_id)
                uuid_str = format_uuid(raw_uuid or data.get("uuid"))
                date_val = data.get("date")
                iso_time = coredata_to_iso(date_val)

                title = str(req.get("titl") or "")
                subtitle = str(req.get("subt") or "")
                body = str(req.get("body") or "")

                notif = {
                    "rec_id": rec_id,
                    "app_id": app_id,
                    "app": app_bundle,
                    "uuid": uuid_str,
                    "timestamp": iso_time,
                    "title": title,
                    "subtitle": subtitle,
                    "body": body,
                    "presented": bool(presented),
                    "raw_data": data,
                }
                results.append(notif)
            return results

        return self.snapshot_and_read(_query)


class StateManager:
    """
    Tracks high-watermark (last_rec_id) and recent UUIDs to deduplicate notifications.
    Persists state atomically to .bot_state.json.
    """

    def __init__(self, state_file: str = ".bot_state.json", max_recent_uuids: int = 1000):
        self.state_file = state_file
        self.max_recent_uuids = max_recent_uuids
        self.last_rec_id: int = 0
        self.recent_uuids: List[str] = []
        self._uuid_set: set = set()
        self.load()

    def load(self) -> None:
        """Load state from state_file if present."""
        if os.path.exists(self.state_file):
            try:
                with open(self.state_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self.last_rec_id = int(data.get("last_rec_id", 0))
                    self.recent_uuids = list(data.get("recent_uuids", []))
                    self._uuid_set = set(self.recent_uuids)
                    logger.debug(
                        "Loaded state: last_rec_id=%d, recent_uuids=%d",
                        self.last_rec_id,
                        len(self.recent_uuids),
                    )
            except Exception as e:
                logger.warning("Failed to load state file %s: %s", self.state_file, e)

    def is_duplicate(self, rec_id: int, notif_uuid: str) -> bool:
        """
        Check if notification has already been processed.
        Duplicate if rec_id <= last_rec_id, or if non-empty uuid is in recent_uuids.
        """
        if rec_id <= self.last_rec_id:
            return True
        if notif_uuid and notif_uuid in self._uuid_set:
            return True
        return False

    def record_seen(self, rec_id: int, notif_uuid: str) -> None:
        """Update high watermark and recent UUID window."""
        if rec_id > self.last_rec_id:
            self.last_rec_id = rec_id
        if notif_uuid and notif_uuid not in self._uuid_set:
            self.recent_uuids.append(notif_uuid)
            self._uuid_set.add(notif_uuid)
            if len(self.recent_uuids) > self.max_recent_uuids:
                overflow = len(self.recent_uuids) - self.max_recent_uuids
                removed = self.recent_uuids[:overflow]
                self.recent_uuids = self.recent_uuids[overflow:]
                for u in removed:
                    self._uuid_set.discard(u)

    def save(self) -> None:
        """Atomically persist state to disk via temporary file rename."""
        data = {
            "last_rec_id": self.last_rec_id,
            "recent_uuids": self.recent_uuids,
            "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }
        state_dir = os.path.dirname(os.path.abspath(self.state_file))
        os.makedirs(state_dir, exist_ok=True)
        tmp_path = f"{self.state_file}.tmp.{os.getpid()}"
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, self.state_file)
            logger.debug("Persisted state: last_rec_id=%d", self.last_rec_id)
        except Exception as e:
            logger.error("Failed to save state to %s: %s", self.state_file, e)
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass


class LLMClassifier:
    """Classifies notifications using local Ollama model with tolerant JSON parsing and non-fatal fallback."""

    SYSTEM_PROMPT = (
        "You are a notification classification engine.\n"
        "Given the notification app, title, subtitle, and body, return a valid JSON object with EXACTLY three keys:\n"
        "- category: string, one of [\"message\", \"alert\", \"promo\", \"system\", \"other\"]\n"
        "- urgency: string, one of [\"high\", \"medium\", \"low\"]\n"
        "- summary: string, concise 1-sentence summary\n"
        "Return ONLY the JSON object. Do not wrap in explanation or conversational text."
    )

    def __init__(
        self,
        url: str = "http://127.0.0.1:11434/api/chat",
        model: str = "glm-5.3-flash:cloud",
        timeout: float = 30.0,
    ):
        self.url = url
        self.model = model
        self.timeout = timeout

    @staticmethod
    def clean_json_text(text: str) -> str:
        """Tolerant cleanup: strip markdown fences and extract first {...} block."""
        s = text.strip()
        # Strip ```json or ``` fences if present
        if s.startswith("```"):
            lines = s.split("\n")
            if lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip().startswith("```"):
                lines = lines[:-1]
            s = "\n".join(lines).strip()
        # Find first { and last }
        start = s.find("{")
        end = s.rfind("}")
        if start != -1 and end != -1 and end > start:
            s = s[start : end + 1]
        return s

    def fallback(self, notif: dict) -> dict:
        """Non-fatal default classification."""
        summary = notif.get("title") or notif.get("body") or "Notification"
        return {
            "category": "other",
            "urgency": "low",
            "summary": summary[:120],
        }

    def classify(self, notif: dict) -> dict:
        """Call Ollama /api/chat with model and tolerant parsing."""
        user_content = (
            f"App: {notif.get('app', '')}\n"
            f"Title: {notif.get('title', '')}\n"
            f"Subtitle: {notif.get('subtitle', '')}\n"
            f"Body: {notif.get('body', '')}"
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
                msg = resp_json.get("message", {})
                raw_content = msg.get("content", "")
                cleaned = self.clean_json_text(raw_content)
                parsed = json.loads(cleaned)
                if isinstance(parsed, dict) and "category" in parsed and "urgency" in parsed:
                    category = str(parsed.get("category", "other")).lower().strip()
                    urgency = str(parsed.get("urgency", "low")).lower().strip()
                    summary = str(
                        parsed.get("summary") or notif.get("title") or notif.get("body") or "Notification"
                    )
                    return {
                        "category": category,
                        "urgency": urgency,
                        "summary": summary,
                    }
                logger.warning("LLM response missing expected keys: %s", raw_content)
                return self.fallback(notif)
        except Exception as e:
            logger.warning("LLM classification failed (%s); using fallback.", e)
            return self.fallback(notif)


class RuleRouter:
    """Evaluates routing rules top-down; first match wins; fallthrough is ['log']."""

    def __init__(self, rules: Optional[List[dict]] = None):
        self.rules = rules or []

    def evaluate(self, notif: dict, classification: dict) -> Tuple[str, List[str]]:
        for rule in self.rules:
            name = rule.get("name", "unnamed")
            cond = rule.get("condition", {})
            actions = rule.get("actions", ["log"])

            if self._matches(cond, notif, classification):
                return name, actions

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


class ActionDispatcher:
    """Dispatches routed actions: log to JSONL & stderr, telegram, external command."""

    def __init__(
        self,
        log_file: str = "logs/events.jsonl",
        telegram_token: str = "",
        telegram_chat_id: str = "",
    ):
        self.log_file = log_file
        self.telegram_token = telegram_token
        self.telegram_chat_id = telegram_chat_id

    def log_event(self, event_data: dict) -> None:
        """Append event to log_file as a JSON line."""
        try:
            log_dir = os.path.dirname(os.path.abspath(self.log_file))
            os.makedirs(log_dir, exist_ok=True)
            with open(self.log_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(event_data, ensure_ascii=False) + "\n")
        except Exception as e:
            logger.error("Failed to write to log_file %s: %s", self.log_file, e)

    def log_startup(self, watermark: int, mode: str = "daemon") -> None:
        """Write a startup event line to log_file."""
        startup_event = {
            "event": "startup",
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "mode": mode,
            "watermark": watermark,
        }
        self.log_event(startup_event)
        logger.info("Bot started in '%s' mode (watermark=%d)", mode, watermark)

    def execute_command(self, cmd_path: str, event_payload: dict) -> str:
        """Run an external command with event JSON in argv[1]. Timeout 30s."""
        try:
            payload_json = json.dumps(event_payload, ensure_ascii=False)
            proc = subprocess.run(
                [cmd_path, payload_json],
                capture_output=True,
                text=True,
                timeout=30.0,
            )
            if proc.returncode == 0:
                return "ok"
            logger.warning(
                "Command '%s' exited %d. stderr: %s",
                cmd_path,
                proc.returncode,
                proc.stderr.strip(),
            )
            return f"exit_{proc.returncode}"
        except subprocess.TimeoutExpired:
            logger.error("Command '%s' timed out after 30s", cmd_path)
            return "timeout"
        except Exception as e:
            logger.error("Command '%s' execution error: %s", cmd_path, e)
            return f"error: {e}"

    def send_telegram(self, notif: dict, classification: dict) -> str:
        """Send message via Telegram Bot API."""
        if not self.telegram_token or not self.telegram_chat_id:
            logger.info("Telegram action SKIPPED (missing token or chat_id)")
            return "skipped"

        cat = classification.get("category", "other").upper()
        urg = classification.get("urgency", "low").upper()
        app = notif.get("app", "Unknown")
        title = notif.get("title", "")
        body = notif.get("body", "")
        summary = classification.get("summary", "")

        text_lines = [
            f"🔔 *[{cat}]* {urg} — `{app}`",
            f"*Title:* {title}",
        ]
        if body:
            text_lines.append(f"{body}")
        if summary and summary != title:
            text_lines.append(f"*Summary:* _{summary}_")
        message_text = "\n".join(text_lines)

        url = f"https://api.telegram.org/bot{self.telegram_token}/sendMessage"
        payload = {
            "chat_id": self.telegram_chat_id,
            "text": message_text,
            "parse_mode": "Markdown",
        }
        try:
            req_data = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(
                url,
                data=req_data,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=10.0) as resp:
                resp_json = json.loads(resp.read().decode("utf-8"))
                if resp_json.get("ok"):
                    return "ok"
                return f"error: {resp_json}"
        except Exception as e:
            logger.warning("Telegram send failed: %s", e)
            return f"error: {e}"

    def dispatch(
        self,
        actions: List[str],
        notif: dict,
        classification: dict,
        rule_name: str,
    ) -> Dict[str, str]:
        """Dispatch actions according to rule results."""
        results: Dict[str, str] = {}
        now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()

        # Build base event payload
        event = {
            "event": "notification",
            "timestamp": now_iso,
            "rec_id": notif.get("rec_id"),
            "app": notif.get("app"),
            "uuid": notif.get("uuid"),
            "title": notif.get("title"),
            "subtitle": notif.get("subtitle"),
            "body": notif.get("body"),
            "classification": classification,
            "rule_matched": rule_name,
            "actions": actions,
        }

        # Telegram action
        if "telegram" in actions:
            status = self.send_telegram(notif, classification)
            results["telegram"] = status
            event["telegram"] = status

        # Command actions
        for act in actions:
            if act.startswith("command:"):
                cmd_path = act[len("command:"):].strip()
                cmd_status = self.execute_command(cmd_path, event)
                results[act] = cmd_status

        # Log action (default and explicit)
        results["log"] = "ok"
        event["action_results"] = results

        # Write to stderr
        readable_line = (
            f"[{now_iso}] [RULE: {rule_name}] [ACTIONS: {','.join(actions)}] "
            f"{notif.get('app', 'app')} - {notif.get('title', '')}: {notif.get('body', '')} "
            f"(Summary: {classification.get('summary', '')})"
        )
        print(readable_line, file=sys.stderr)

        # Write to log_file
        self.log_event(event)
        return results


def run_cycle(
    reader: NotificationReader,
    state_mgr: StateManager,
    classifier: LLMClassifier,
    router: RuleRouter,
    dispatcher: ActionDispatcher,
    is_startup: bool = False,
    mode: str = "daemon",
) -> int:
    """Run a single polling cycle, processing any new notifications."""
    if is_startup:
        dispatcher.log_startup(state_mgr.last_rec_id, mode=mode)

    try:
        notifications = reader.fetch_notifications(
            since_rec_id=state_mgr.last_rec_id, reverse=False
        )
    except Exception as e:
        logger.error("Failed to read notifications: %s", e)
        return 0

    processed_count = 0
    for notif in notifications:
        rec_id = notif.get("rec_id", 0)
        notif_uuid = notif.get("uuid", "")

        if state_mgr.is_duplicate(rec_id, notif_uuid):
            # Advance watermark even for duplicate presentations so we don't query it again
            state_mgr.record_seen(rec_id, notif_uuid)
            continue

        classification = classifier.classify(notif)
        rule_name, actions = router.evaluate(notif, classification)
        dispatcher.dispatch(actions, notif, classification, rule_name)

        state_mgr.record_seen(rec_id, notif_uuid)
        state_mgr.save()
        processed_count += 1

    if processed_count > 0:
        state_mgr.save()

    return processed_count


def load_config(config_path: str) -> dict:
    """Load configuration from JSON file or return defaults."""
    defaults = {
        "poll_interval": 2.0,
        "state_file": ".bot_state.json",
        "log_file": "logs/events.jsonl",
        "ollama": {
            "url": "http://127.0.0.1:11434/api/chat",
            "model": "glm-5.3-flash:cloud",
            "timeout": 30.0,
        },
        "telegram": {
            "token": "",
            "chat_id": "",
        },
        "rules": [
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
        ],
    }

    if os.path.exists(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                loaded = json.load(f)
                defaults.update(loaded)
        except Exception as e:
            logger.warning("Could not read config %s: %s; using defaults", config_path, e)
    return defaults


def trigger_test_notifications() -> None:
    """Post 3 demo notifications via osascript for testing and verification."""
    tests = [
        {
            "title": "Bank Security Alert",
            "subtitle": "Urgent Transaction",
            "body": "Suspicious charge of $450 detected on card ending 1234. Immediate action required.",
        },
        {
            "title": "Alice (Messages)",
            "subtitle": "iMessage",
            "body": "Are we still meeting for lunch today at 1pm?",
        },
        {
            "title": "App Store",
            "subtitle": "Weekend Special",
            "body": "Get 50% off productivity tools during our annual sale!",
        },
    ]

    for item in tests:
        script = (
            f'display notification "{item["body"]}" '
            f'with title "{item["title"]}" '
            f'subtitle "{item["subtitle"]}"'
        )
        try:
            subprocess.run(["osascript", "-e", script], check=True)
            print(f"Triggered notification: {item['title']} - {item['subtitle']}")
        except Exception as e:
            print(f"Failed to post notification via osascript: {e}", file=sys.stderr)
        time.sleep(1)


def dump_notifications(reader: NotificationReader, count: int = 10) -> None:
    """Print recent parsed notifications to stdout."""
    try:
        notifications = reader.fetch_notifications(since_rec_id=0, limit=count, reverse=True)
    except Exception as e:
        print(f"Error reading notifications: {e}", file=sys.stderr)
        return

    if not notifications:
        print("No notifications found in database.")
        return

    print(f"--- Recent Notifications ({len(notifications)}) ---")
    for n in notifications:
        print(
            f"[{n['rec_id']}] {n['timestamp'] or 'No timestamp'} | App: {n['app'] or 'Unknown'}\n"
            f"  Title:    {n['title']}\n"
            f"  Subtitle: {n['subtitle']}\n"
            f"  Body:     {n['body']}\n"
            f"  UUID:     {n['uuid']}\n"
        )


def main():
    parser = argparse.ArgumentParser(description="macOS Notification Bot")
    parser.add_argument("--config", default="config.json", help="Path to config.json")
    parser.add_argument(
        "--dump",
        nargs="?",
        const=10,
        type=int,
        metavar="N",
        help="Print the last N notifications (default 10) and exit",
    )
    parser.add_argument(
        "--test-notify",
        action="store_true",
        help="Trigger 3 test notifications via osascript and exit",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single polling cycle, write logs, and exit",
    )
    parser.add_argument(
        "--db-path",
        default=None,
        help="Override path to Notification Center db2 file",
    )
    parser.add_argument(
        "--watermark",
        type=int,
        default=None,
        help="Override starting watermark rec_id",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    if args.test_notify:
        trigger_test_notifications()
        sys.exit(0)

    reader = NotificationReader(db_path=args.db_path)

    if args.dump is not None:
        dump_notifications(reader, count=args.dump)
        sys.exit(0)

    config = load_config(args.config)
    state_file = config.get("state_file", ".bot_state.json")
    log_file = config.get("log_file", "logs/events.jsonl")

    state_mgr = StateManager(state_file=state_file)
    if args.watermark is not None:
        state_mgr.last_rec_id = args.watermark

    ollama_cfg = config.get("ollama", {})
    classifier = LLMClassifier(
        url=ollama_cfg.get("url", "http://127.0.0.1:11434/api/chat"),
        model=ollama_cfg.get("model", "glm-5.3-flash:cloud"),
        timeout=float(ollama_cfg.get("timeout", 30.0)),
    )

    tg_token, tg_chat_id = resolve_telegram_config(config.get("telegram"))
    dispatcher = ActionDispatcher(
        log_file=log_file,
        telegram_token=tg_token,
        telegram_chat_id=tg_chat_id,
    )

    router = RuleRouter(rules=config.get("rules", []))

    if args.once:
        run_cycle(
            reader=reader,
            state_mgr=state_mgr,
            classifier=classifier,
            router=router,
            dispatcher=dispatcher,
            is_startup=True,
            mode="once",
        )
        sys.exit(0)

    # Daemon mode
    running = True

    def _handle_exit(signum, frame):
        nonlocal running
        running = False
        logger.info("Received signal %d; shutting down cleanly...", signum)

    signal.signal(signal.SIGINT, _handle_exit)
    signal.signal(signal.SIGTERM, _handle_exit)

    poll_interval = float(config.get("poll_interval", 2.0))
    run_cycle(
        reader=reader,
        state_mgr=state_mgr,
        classifier=classifier,
        router=router,
        dispatcher=dispatcher,
        is_startup=True,
        mode="daemon",
    )

    while running:
        try:
            time.sleep(poll_interval)
            if not running:
                break
            run_cycle(
                reader=reader,
                state_mgr=state_mgr,
                classifier=classifier,
                router=router,
                dispatcher=dispatcher,
                is_startup=False,
                mode="daemon",
            )
        except Exception as e:
            logger.error("Error in daemon polling loop: %s", e)

    state_mgr.save()
    logger.info("Bot daemon stopped.")
    sys.exit(0)


if __name__ == "__main__":
    main()
