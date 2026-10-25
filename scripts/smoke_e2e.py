#!/usr/bin/env python3
"""End-to-end smoke test for the macOS notification bot.

Step 0 reuses complete evidence from a previous run (no post, no bot run).
Otherwise it posts two osascript notifications, runs ``bot.py --once`` (at
most twice), and verifies received/classified/routed evidence in the JSONL
log. The script only READS ``logs/events.jsonl`` and never touches
``.bot_state.json``. Telegram SKIPPED is acceptable and never a failure.
"""

import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

MARKERS: List[Tuple[str, str]] = [
    ("SMOKE-E2E-1",
     "smoke e2e alpha: your package has shipped and promo code E2E20 is active"),
    ("SMOKE-E2E-2",
     "smoke e2e bravo: urgent security alert - verify your account now"),
]
TELEGRAM_SKIP_TEXT = "Telegram action SKIPPED"
EVIDENCE_LIMIT = 240


def truncate(text: str, limit: int = EVIDENCE_LIMIT) -> str:
    """Trim evidence to a readable single line."""
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def print_check(name: str, passed: bool, evidence: str) -> bool:
    """Emit ``[PASSED/FAILED] <name> — <evidence>``."""
    print("[%s] %s \u2014 %s" % ("PASSED" if passed else "FAILED", name, evidence))
    return passed


def read_events(log_path: Path) -> List[Dict[str, Any]]:
    """Read valid JSONL records; missing file or malformed lines are skipped."""
    records: List[Dict[str, Any]] = []
    if not log_path.exists():
        return records
    try:
        with log_path.open("r", encoding="utf-8", errors="replace") as handle:
            for line_no, raw in enumerate(handle, 1):
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    event = json.loads(raw)
                except (ValueError, TypeError):
                    continue
                if isinstance(event, dict):
                    records.append({"line_no": line_no, "raw": raw, "event": event})
    except OSError:
        return []
    return records


def evaluate_record(record: Dict[str, Any]) -> Dict[str, Any]:
    """Apply step-4 acceptance checks to one notification record."""
    event = record.get("event") or {}
    cls = event.get("classification")
    cls = cls if isinstance(cls, dict) else {}
    summary = cls.get("summary")
    actions = event.get("actions")
    results = event.get("action_results")
    results = results if isinstance(results, dict) else {}
    rule = event.get("rule_matched")
    classified = isinstance(summary, str) and summary.strip() != ""
    routed = (
        isinstance(rule, str) and rule.strip() != ""
        and isinstance(actions, list) and len(actions) > 0
        and results.get("log") == "ok"
    )
    return {
        "classified": classified,
        "routed": routed,
        "summary": summary if isinstance(summary, str) else "",
        "category": cls.get("category"),
        "urgency": cls.get("urgency"),
        "rule": rule if isinstance(rule, str) else "",
        "actions": actions if isinstance(actions, list) else [],
        "telegram": event.get("telegram", results.get("telegram")),
    }


def best_marker_event(
    records: List[Dict[str, Any]], marker: str
) -> Optional[Dict[str, Any]]:
    """Return the most complete (then latest) notification for a marker."""
    best, best_score = None, -1
    for record in records:
        event = record.get("event") or {}
        if event.get("event") != "notification":
            continue
        haystack = "%s %s" % (event.get("title") or "", event.get("body") or "")
        if marker not in haystack:
            continue
        checks = evaluate_record(record)
        score = int(checks["classified"]) + int(checks["routed"])
        if score >= best_score:  # tie-break: keep the latest record
            best, best_score = record, score
    return best


def has_full_evidence(record: Optional[Dict[str, Any]]) -> bool:
    """Received + classified + routed."""
    if record is None:
        return False
    checks = evaluate_record(record)
    return bool(checks["classified"] and checks["routed"])


def find_startup(records: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Find any startup event emitted in --once mode."""
    for record in records:
        event = record.get("event") or {}
        if event.get("event") == "startup" and event.get("mode") == "once":
            return record
    return None


def post_notification(repo_root: Path, title: str, body: str) -> bool:
    """Post one notification via osascript; True on success."""
    script = 'display notification "%s" with title "%s"' % (body, title)
    try:
        proc = subprocess.run(
            ["osascript", "-e", script], cwd=str(repo_root),
            capture_output=True, text=True, timeout=15,
        )
    except subprocess.TimeoutExpired:
        return print_check("post[%s]" % title, False, "osascript timed out after 15s")
    except OSError as exc:
        return print_check("post[%s]" % title, False, "osascript failed: %s" % exc)
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip()
        return print_check(
            "post[%s]" % title, False,
            "osascript exit %d: %s" % (proc.returncode, truncate(tail, 400)),
        )
    return print_check("post[%s]" % title, True, "posted via osascript")


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    return value.decode("utf-8", "replace") if isinstance(value, bytes) else str(value)


def run_bot(repo_root: Path) -> Dict[str, Any]:
    """Run one ``bot.py --once``; never raises."""
    try:
        proc = subprocess.run(
            [sys.executable, "bot.py", "--once"], cwd=str(repo_root),
            capture_output=True, text=True, timeout=180,
        )
        return {"ok": proc.returncode == 0, "returncode": proc.returncode,
                "stdout": _as_text(proc.stdout), "stderr": _as_text(proc.stderr),
                "error": ""}
    except subprocess.TimeoutExpired as exc:
        return {"ok": False, "returncode": None, "stdout": _as_text(exc.stdout),
                "stderr": _as_text(exc.stderr), "error": "bot.py timed out after 180s"}
    except OSError as exc:
        return {"ok": False, "returncode": None, "stdout": "", "stderr": "",
                "error": "bot.py failed to start: %s" % exc}


def report_bot_run(number: int, result: Dict[str, Any]) -> bool:
    """Report a bot invocation plus any Telegram SKIPPED output."""
    output = (result["stdout"] + "\n" + result["stderr"]).strip()
    skipped = TELEGRAM_SKIP_TEXT in output
    if result["ok"]:
        note = "exit 0"
        if skipped:
            note += "; output contained %r" % TELEGRAM_SKIP_TEXT
        return print_check("bot run %d" % number, True, note)
    if result["error"]:
        evidence = result["error"]
    else:
        tail = (result["stderr"] or result["stdout"]).strip()
        evidence = "exit %s; stderr tail: %s" % (
            result["returncode"], truncate(tail, 400) or "(empty)")
    return print_check("bot run %d" % number, False, evidence)


def report_marker(marker: str, record: Optional[Dict[str, Any]]) -> bool:
    """Print received/classified/routed/telegram checks for one marker."""
    if record is None:
        checks = [
            ("received[%s]" % marker, False, "no notification event with marker"),
            ("classified[%s]" % marker, False, "no notification event"),
            ("routed[%s]" % marker, False, "no notification event"),
            ("telegram[%s]" % marker, True,
             "no event, nothing to report (never a failure)"),
        ]
        for item in checks:
            print_check(*item)
        return False

    raw = truncate(record["raw"])
    c = evaluate_record(record)
    checks = [
        ("received[%s]" % marker, True,
         "line %d: %s" % (record["line_no"], raw)),
        ("classified[%s]" % marker, c["classified"],
         "summary=%r category=%r urgency=%r | %s"
         % (c["summary"], c["category"], c["urgency"], raw)
         if c["classified"] else "classification.summary empty/missing | %s" % raw),
        ("routed[%s]" % marker, c["routed"],
         "rule_matched=%r actions=%r action_results.log='ok' | %s"
         % (c["rule"], c["actions"], raw)
         if c["routed"] else "incomplete routing evidence | %s" % raw),
    ]
    telegram = c["telegram"]
    if telegram == "skipped":
        checks.append(("telegram[%s]" % marker, True,
                       "SKIPPED (missing credentials, acceptable)"))
    elif telegram is None:
        checks.append(("telegram[%s]" % marker, True,
                       "not routed to telegram for this rule (acceptable)"))
    elif telegram == "ok":
        checks.append(("telegram[%s]" % marker, True, "ok"))
    else:  # e.g. "error: ..." — reported, but never a failure
        checks.append(("telegram[%s]" % marker, True,
                       "%s (non-fatal; telegram is never a failure)"
                       % truncate(str(telegram))))
    for item in checks:
        print_check(*item)
    return c["classified"] and c["routed"]


def report_startup(records: List[Dict[str, Any]]) -> bool:
    """Verify at least one startup event with mode ``once`` exists."""
    record = find_startup(records)
    if record is None:
        return print_check(
            "startup", False, "no startup event with mode 'once' in logs/events.jsonl")
    return print_check(
        "startup", True, "mode=once timestamp=%s (line %d)"
        % (record["event"].get("timestamp"), record["line_no"]))


def main() -> int:
    repo_root = Path(__file__).resolve().parent.parent
    log_path = repo_root / "logs" / "events.jsonl"
    records = read_events(log_path)
    existing = {marker: best_marker_event(records, marker) for marker, _ in MARKERS}
    all_ok = True

    if all(has_full_evidence(existing[marker]) for marker, _ in MARKERS):
        # Step 0: idempotent reuse — no notification posted, no bot run.
        all_ok &= print_check(
            "reuse", True,
            "found complete evidence for SMOKE-E2E-1/SMOKE-E2E-2 from a "
            "previous run; no new notifications posted")
    else:
        for marker, body in MARKERS:  # always attempt both posts
            all_ok &= post_notification(repo_root, marker, body)
        time.sleep(1.5)  # allow the db2 write to land
        all_ok &= report_bot_run(1, run_bot(repo_root))
        records = read_events(log_path)

        missing = [m for m, _ in MARKERS if best_marker_event(records, m) is None]
        if missing:  # bounded retry: at most two bot invocations total
            time.sleep(3)
            all_ok &= report_bot_run(2, run_bot(repo_root))
            records = read_events(log_path)

    for marker, _ in MARKERS:
        all_ok &= report_marker(marker, best_marker_event(records, marker))
    all_ok &= report_startup(records)

    print("SMOKE RESULT: %s" % ("PASSED" if all_ok else "FAILED"))
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
