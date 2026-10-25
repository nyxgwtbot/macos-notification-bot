"""Notification Center db2 snapshot reader."""

import logging
import os
import shutil
import sqlite3
import subprocess
import tempfile
import time
from typing import Dict, List, Optional

from notifybot.parser import coredata_to_iso, format_uuid, parse_plist_blob

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