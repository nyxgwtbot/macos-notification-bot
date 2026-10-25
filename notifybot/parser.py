"""Low-level parsing helpers: binary plists, CoreData timestamps, binary UUIDs."""

import datetime
import plistlib
import uuid
from typing import Any, Optional

COREDATA_EPOCH_OFFSET = 978307200  # seconds between 1970-01-01 and 2001-01-01
COREDATA_EPOCH = datetime.datetime(2001, 1, 1, tzinfo=datetime.timezone.utc)


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