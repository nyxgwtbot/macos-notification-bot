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
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid
from typing import Dict, List, Optional, Tuple

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


def format_uuid(val: Optional[bytes]) -> str:
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
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    if args.test_notify:
        trigger_test_notifications()
        sys.exit(0)

    reader = NotificationReader()

    if args.dump is not None:
        dump_notifications(reader, count=args.dump)
        sys.exit(0)

    print("bot.py ready (use --dump or --test-notify). Full daemon implementation in progress.")


if __name__ == "__main__":
    main()
