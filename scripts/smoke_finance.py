#!/usr/bin/env python3
"""End-to-end smoke test for the finance plugin (Router v2 + finance action).

Posts 3 synthetic bank notifications with distinct R$ amounts via osascript,
runs ``bot.py --once`` (bounded to 2 runs, 2-6 s db2 settle time respected),
verifies the rows landed in ``finance.db``, generates the monthly report via
``bot.py report``, and asserts ``reports/<month>.html`` contains three or
more ``<svg`` elements and an ``Insights`` section.

Idempotency (LLM-call budget): markers whose complete evidence already
exists (notification event with router layer + ``finance`` status ``ok`` AND
a ledger row whose raw JSON contains the marker) are never re-posted, so a
rerun consumes zero LLM calls. The HTML report is only regenerated when
missing. Telegram is never asserted (report-only, by design).
"""

import json
import subprocess
import sys
import time
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

MARKERS: List[Dict[str, str]] = [
    {
        "marker": "SMOKE-FIN-1",
        "title": "SMOKE-FIN-1 Banco",
        "body": "SMOKE-FIN-1 compra no cartao final 1234: R$ 123,45 no ifood",
        "amount": "123.45",
        "brl": "R$ 123,45",
    },
    {
        "marker": "SMOKE-FIN-2",
        "title": "SMOKE-FIN-2 Banco",
        "body": "SMOKE-FIN-2 pix recebido de R$ 200,00 de Maria Souza",
        "amount": "200.00",
        "brl": "R$ 200,00",
    },
    {
        "marker": "SMOKE-FIN-3",
        "title": "SMOKE-FIN-3 Banco",
        "body": "SMOKE-FIN-3 pagamento da fatura: R$ 1.234,56 vencimento ok",
        "amount": "1234.56",
        "brl": "R$ 1.234,56",
    },
]
SETTLE_SECONDS = 6.0
EVIDENCE_LIMIT = 200


def truncate(text: Any, limit: int = EVIDENCE_LIMIT) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def print_check(name: str, passed: bool, evidence: str) -> bool:
    print("[%s] %s \u2014 %s" % ("PASSED" if passed else "FAILED", name, evidence))
    return passed


def read_events(log_path: Path) -> List[Dict[str, Any]]:
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


def marker_event(
    records: List[Dict[str, Any]], marker: str
) -> Optional[Dict[str, Any]]:
    """Most complete (then latest) notification event containing the marker."""
    best, best_score = None, -1
    for record in records:
        event = record.get("event") or {}
        if event.get("event") != "notification":
            continue
        haystack = "%s %s" % (event.get("title") or "", event.get("body") or "")
        if marker not in haystack:
            continue
        layer = event.get("layer")
        routed = (
            (layer in ("rule", "llm_router", "fallback_in_fallback"))
            or (isinstance(event.get("rule_matched"), str)
                and event.get("rule_matched").strip() != "")
        )
        finance = (event.get("finance") or {})
        finance_ok = isinstance(finance, dict) and finance.get("status") == "ok"
        score = int(routed) + int(finance_ok)
        if score >= best_score:  # tie-break: keep the latest record
            best, best_score = record, score
    return best


def event_routing_ok(event: Optional[Dict[str, Any]]) -> bool:
    if not event:
        return False
    layer = event.get("layer")
    if layer in ("rule", "llm_router", "fallback_in_fallback"):
        return True
    rule = event.get("rule_matched")
    return isinstance(rule, str) and rule.strip() != "" and rule != "none"


def event_finance_ok(event: Optional[Dict[str, Any]]) -> bool:
    if not event:
        return False
    finance = event.get("finance")
    return isinstance(finance, dict) and finance.get("status") == "ok"


def ledger_rows_by_marker(db_path: Path, marker: str) -> List[Dict[str, Any]]:
    """Rows in finance.db whose raw_json embeds the marker text."""
    if not db_path.exists():
        return []
    import sqlite3

    rows: List[Dict[str, Any]] = []
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        cur = conn.execute(
            "SELECT rec_id, ts, amount, kind, category, description, raw_json "
            "FROM expenses ORDER BY rec_id ASC"
        )
        for rec_id, ts, amount, kind, category, description, raw_json in cur.fetchall():
            if isinstance(raw_json, str) and marker in raw_json:
                rows.append(
                    {
                        "rec_id": rec_id,
                        "ts": ts,
                        "amount": Decimal(str(amount)),
                        "kind": kind,
                        "category": category,
                        "description": description,
                    }
                )
    finally:
        conn.close()
    return rows


def marker_complete(
    records: List[Dict[str, Any]], db_path: Path, item: Dict[str, str]
) -> bool:
    """Full evidence: routed + finance-ingested event + matching ledger row."""
    event = marker_event(records, item["marker"])
    if not (event_routing_ok(event) and event_finance_ok(event)):
        return False
    rows = ledger_rows_by_marker(db_path, item["marker"])
    return any(r["amount"] == Decimal(item["amount"]) for r in rows)


def post_notification(repo_root: Path, item: Dict[str, str]) -> bool:
    script = 'display notification "%s" with title "%s"' % (
        item["body"], item["title"]
    )
    try:
        proc = subprocess.run(
            ["osascript", "-e", script], cwd=str(repo_root),
            capture_output=True, text=True, timeout=15,
        )
    except subprocess.TimeoutExpired:
        return print_check("post[%s]" % item["marker"], False, "osascript timed out")
    except OSError as exc:
        return print_check("post[%s]" % item["marker"], False, "osascript failed: %s" % exc)
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip()
        return print_check(
            "post[%s]" % item["marker"], False,
            "osascript exit %d: %s" % (proc.returncode, truncate(tail)),
        )
    return print_check("post[%s]" % item["marker"], True, "posted via osascript")


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    return value.decode("utf-8", "replace") if isinstance(value, bytes) else str(value)


def run_bot(repo_root: Path, mode: str = "--once") -> Dict[str, Any]:
    """Run one bot.py subprocess in the repo; never raises."""
    try:
        proc = subprocess.run(
            [sys.executable, "bot.py", mode], cwd=str(repo_root),
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
    output = (result["stdout"] + "\n" + result["stderr"]).strip()
    if result["ok"]:
        note = "exit 0"
        if "IGNORADA (token ou chat_id ausentes)" in output:
            note += "; telegram SKIPPED (acceptable)"
        return print_check("bot run %d" % number, True, note)
    evidence = result["error"]
    if not evidence:
        tail = (result["stderr"] or result["stdout"]).strip()
        evidence = "exit %s; stderr tail: %s" % (
            result["returncode"], truncate(tail, 400) or "(empty)")
    return print_check("bot run %d" % number, False, evidence)


def main() -> int:
    repo_root = Path(__file__).resolve().parent.parent
    log_path = repo_root / "logs" / "events.jsonl"
    db_path = repo_root / "finance.db"
    month = datetime.now().astimezone().strftime("%Y-%m")
    report_path = repo_root / "reports" / f"{month}.html"
    all_ok = True

    records = read_events(log_path)
    pending = [item for item in MARKERS if not marker_complete(records, db_path, item)]

    if not pending:
        # Step 0: idempotent reuse — zero notifications posted, zero LLM calls.
        all_ok &= print_check(
            "reuse", True,
            "complete routed+finance+ledger evidence already exists for all "
            "3 markers; no new notifications posted",
        )
    else:
        for item in pending:  # only markers still lacking evidence
            all_ok &= post_notification(repo_root, item)
        time.sleep(SETTLE_SECONDS)  # db2 rows need ~2-6 s to settle
        all_ok &= report_bot_run(1, run_bot(repo_root))
        records = read_events(log_path)

        still_missing = [
            item for item in pending if not marker_complete(records, db_path, item)
        ]
        if still_missing:  # bounded retry: at most two bot runs total
            time.sleep(2)
            all_ok &= report_bot_run(2, run_bot(repo_root))
            records = read_events(log_path)

    # Per-marker evidence: routing layer, finance ingestion, ledger row.
    for item in MARKERS:
        marker = item["marker"]
        event = marker_event(records, marker)
        routed = event_routing_ok(event)
        finance_ingested = event_finance_ok(event)
        rows = ledger_rows_by_marker(db_path, marker)
        amount_rows = [r for r in rows if r["amount"] == Decimal(item["amount"])]

        if event is None:
            all_ok &= print_check(f"routed[{marker}]", False, "no notification event with marker")
        else:
            layer = event.get("layer")
            all_ok &= print_check(
                f"routed[{marker}]", routed,
                "layer=%s actions=%s confidence=%s | %s"
                % (layer, event.get("actions"), event.get("confidence"),
                   truncate(event["raw"]))
                if routed else "no routing evidence | %s" % truncate(event["raw"]),
            )
        all_ok &= print_check(
            f"finance[{marker}]", finance_ingested,
            "finance event: %s" % truncate((event or {}).get("finance"))
            if finance_ingested else "no finance OK evidence in event",
        )
        if amount_rows:
            row = amount_rows[0]
            all_ok &= print_check(
                f"ledger[{marker}]", True,
                "rec_id=%s ts=%s amount=%s kind=%s category=%s"
                % (row["rec_id"], truncate(row["ts"]), row["amount"],
                   row["kind"], row["category"]),
            )
        else:
            all_ok &= print_check(
                f"ledger[{marker}]", False,
                "no finance.db row with amount %s (rows found: %d)"
                % (item["brl"], len(rows)),
            )

    # Report generation: reuse the existing HTML when present, else generate.
    if not report_path.exists():
        all_ok &= report_bot_run("report", run_bot(repo_root, mode="report"))
    html = ""
    if report_path.exists():
        try:
            html = report_path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            all_ok &= print_check("report", False, "unreadable: %s" % exc)
    else:
        all_ok &= print_check("report", False, "reports/%s.html missing" % month)

    all_ok &= print_check(
        "report[svg>=3]", html.count("<svg") >= 3,
        "found %d '<svg' occurrences in %s" % (html.count("<svg"), report_path.name),
    )
    all_ok &= print_check(
        "report[insights]", "Insights" in html,
        'section "Insights" present' if "Insights" in html else "no Insights section",
    )
    for item in MARKERS:
        found = item["brl"] in html
        all_ok &= print_check(
            f"report[amount {item['brl']}]", found,
            "amount visible in report" if found else "amount not rendered in report",
        )

    print("SMOKE RESULT: %s" % ("PASSED" if all_ok else "FAILED"))
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())