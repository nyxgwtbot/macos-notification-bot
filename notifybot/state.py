"""Watermark tracking and UUID deduplication with atomic state persistence."""

import datetime
import json
import logging
import os
from typing import List

logger = logging.getLogger("notification_bot")


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