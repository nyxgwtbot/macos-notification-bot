#!/usr/bin/env python3
"""Smoke test for the Desktop UI JSON API (`python3 bot.py api`).

Boots ``bot.py api`` on a fixed loopback port against a THROWAWAY sandbox
repo (tempdir config.json + logs/ + finance.db), asserts every endpoint's
response shape, prints ``[PASSED]/[FAILED]`` lines mirroring
scripts/smoke_e2e.py, and prints ``SMOKE RESULT: PASSED|FAILED``.

The smoke never touches the real config.json / logs/events.jsonl /
finance.db / .bot_state.json: config paths are sandboxed, and the two
side-effectful POSTs (report build) run only inside the sandbox.
POST /api/test-notify posts REAL macOS notifications, so it is skipped
unless NOTIFYBOT_SMOKE_TEST_NOTIFY=1 is set. Idempotent: a fresh tempdir
per run; the server process is always terminated.
"""

import contextlib
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)  # repo-root imports work regardless of CWD

from notifybot.finance import FinanceLedger
from notifybot.report import validate_month  # noqa: F401  (contract reference)

PREFERRED_PORT = 4770
HEALTH_TIMEOUT_S = 20.0


def print_check(name: str, passed: bool, evidence: str) -> bool:
    """Emit ``[PASSED/FAILED] <name> — <evidence>`` (smoke_e2e style)."""
    print("[%s] %s \u2014 %s" % ("PASSED" if passed else "FAILED", name, evidence))
    return passed


def free_port(preferred: int = PREFERRED_PORT) -> int:
    """First free port among preferred..preferred+9."""
    for candidate in range(preferred, preferred + 10):
        with contextlib.closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as sock:
            try:
                sock.bind(("127.0.0.1", candidate))
                return candidate
            except OSError:
                continue
    raise RuntimeError(f"nenhuma porta livre entre {preferred} e {preferred + 9}")


def sandbox_config() -> dict:
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
            {"id": "log", "match_hint": "Registrar sem consequências.", "type": "builtin",
             "enabled": True, "config": {}},
            {"id": "example_command", "match_hint": "Exemplo de comando externo.", "type": "command",
             "enabled": False, "config": {"command": "/path/to/prog"}},
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


def seed_sandbox(root: str) -> Tuple[str, str]:
    """Write sandbox config + finance.db + events.jsonl (with a malformed line)."""
    config_path = os.path.join(root, "config.json")
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(sandbox_config(), f)

    from decimal import Decimal

    ledger = FinanceLedger(db_path=os.path.join(root, "finance.db"))
    ledger.insert_transaction(
        {"rec_id": 901, "ts": "2026-10-05T12:00:00-03:00", "bundle": "com.bank",
         "amount": Decimal("123.45"), "kind": "gasto", "category": "alimentacao",
         "description": "compra de teste", "raw": {}}
    )
    ledger.insert_transaction(
        {"rec_id": 902, "ts": "2026-09-28T10:00:00-03:00", "bundle": "com.bank",
         "amount": Decimal("100.00"), "kind": "gasto", "category": "transporte",
         "description": "transporte de teste", "raw": {}}
    )
    ledger.close()

    logs_dir = os.path.join(root, "logs")
    os.makedirs(logs_dir, exist_ok=True)
    events = [
        json.dumps({"event": "notification", "timestamp": "2026-10-26T10:00:00Z", "rec_id": 10,
                    "app": "com.apple.mail", "title": "Mail msg", "body": "corpo",
                    "classification": {"category": "message", "urgency": "low", "summary": "resumo a"},
                    "layer": "rule", "rule_matched": "messages", "confidence": None,
                    "router_payload": {}, "actions": ["log"], "action_results": {"log": "ok"}}),
        "linhas-mal-formadas-são-toleradas-e-não-derrete-nada",
        json.dumps({"event": "startup", "timestamp": "2026-10-26T09:00:00Z", "mode": "daemon", "watermark": 10}),
    ]
    with open(os.path.join(logs_dir, "events.jsonl"), "w", encoding="utf-8") as f:
        f.write("\n".join(events) + "\n")
    return config_path


def api_request(base: str, method: str, path: str, body: Optional[dict] = None) -> Tuple[int, Any]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        base + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"} if data else {},
    )
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8")
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, {"raw": raw}


def main() -> int:
    all_ok = True
    port = free_port()
    base = f"http://127.0.0.1:{port}"

    with tempfile.TemporaryDirectory(prefix="notifybot-api-smoke-") as root:
        config_path = seed_sandbox(root)

        env = dict(os.environ)
        env.pop("NOTIFYBOT_API_PORT", None)  # --port wins for the sandbox server
        proc = subprocess.Popen(
            [sys.executable, "bot.py", "api", "--config", config_path, "--port", str(port)],
            cwd=REPO_ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        try:
            # Wait for /api/health to answer before asserting shapes.
            deadline = time.time() + HEALTH_TIMEOUT_S
            first_health: Optional[dict] = None
            while time.time() < deadline:
                try:
                    status, body = api_request(base, "GET", "/api/health")
                    if status == 200:
                        first_health = body
                        break
                except (OSError, urllib.error.URLError):
                    time.sleep(0.4)
            if first_health is None:
                print("SMOKE RESULT: FAILED (server nunca subiu)")
                return 1

            # #1 /api/health
            body = first_health
            all_ok &= print_check(
                "health", body.get("ok") is True and all(
                    key in body for key in ("bot_version", "api_version", "watermark", "log", "finance", "ollama")
                ),
                f"watermark={body['watermark']} finance={body['finance']}",
            )
            all_ok &= print_check(
                "health watermark", body["watermark"].get("last_rec_id") == 0,
                "state ausente no sandbox -> last_rec_id 0",
            )

            # #2 /api/events
            status, body = api_request(base, "GET", "/api/events?limit=50")
            all_ok &= print_check(
                "events", status == 200 and body["count"] == 2 and body["latest_rec_id"] == 10,
                f"count={body['count']} latest={body['latest_rec_id']} (linha mal-formada ignorada)",
            )
            status, body = api_request(base, "GET", "/api/events?since=10")
            all_ok &= print_check("events since", status == 200 and body["count"] == 0,
                                  "cursor avança até o fim")
            status, body = api_request(base, "GET", "/api/events?kind=startup")
            all_ok &= print_check("events kind", status == 200 and body["count"] == 1, "startup isolado")
            status, body = api_request(base, "GET", "/api/events?limit=0")
            all_ok &= print_check("events limit 0 -> 400", status == 400, str(body))

            # #3 /api/config masked
            status, body = api_request(base, "GET", "/api/config")
            all_ok &= print_check(
                "config get", status == 200 and body["telegram"] == {"token": "", "chat_id": ""}
                and "actions" in body and "rules" in body,
                "seções completas, telegram vazio",
            )

            # #4 POST /api/config — invalid then valid+reload
            bad = sandbox_config()
            bad["poll_interval"] = 0
            status, body = api_request(base, "POST", "/api/config", bad)
            all_ok &= print_check("config post inválido", status == 400 and "details" in body,
                                  str(body.get("details"))[:120])

            original = open(config_path, "r", encoding="utf-8").read()
            all_ok &= print_check("config arquivo intacto pós-falha", json.loads(original)["poll_interval"] == 2.0,
                                  "nenhum write parcial")

            good = sandbox_config()
            good["poll_interval"] = 4.5
            status, body = api_request(base, "POST", "/api/config", good)
            _, reloaded = api_request(base, "GET", "/api/config")
            all_ok &= print_check("config post válido + reload", status == 200 and body.get("ok") is True
                                  and reloaded["poll_interval"] == 4.5,
                                  "escrita atômica relida pela própria API")

            # #5–#7 /api/finance/*
            status, body = api_request(base, "GET", "/api/finance/summary?month=2026-10")
            all_ok &= print_check(
                "finance summary", status == 200 and body["db_exists"] is True
                and abs(body["total"] - 123.45) < 0.01 and body["count"] == 1
                and abs(body["prev_total"] - 100.0) < 0.01 and abs(body["mom_delta"] - 23.45) < 0.1,
                f"total={body['total']} prev={body['prev_total']} mom={body['mom_delta']}",
            )
            status, body = api_request(base, "GET", "/api/finance/transactions?month=2026-10")
            all_ok &= print_check(
                "finance transactions", status == 200 and len(body["records"]) == 1
                and set(body["records"][0]) >= {"rec_id", "ts", "bundle", "amount", "kind", "category", "description"},
                "registro completo sem raw_json",
            )
            status, body = api_request(base, "GET", "/api/finance/report?month=2077-13")
            all_ok &= print_check("finance report mês ruim -> 400", status == 400, str(body))

            # #8 POST /api/finance/report (LLM real local com fallback determinístico)
            status, body = api_request(base, "POST", "/api/finance/report", {"month": "2026-10"})
            html_ok = status == 200 and body.get("ok") is True
            all_ok &= print_check("finance report build", html_ok,
                                  f"html_path={body.get('html_path')}" if html_ok else str(body))
            if html_ok:
                exists = os.path.exists(body["html_path"])
                all_ok &= print_check("finance report arquivo", exists, body["html_path"])
                status, body2 = api_request(base, "GET", "/api/finance/report?month=2026-10")
                all_ok &= print_check("finance report status pós-build", body2.get("exists") is True,
                                      "GET report vê o arquivo gerado")

            # #10 rota errada / método errado
            status, body = api_request(base, "GET", "/api/inexistente")
            all_ok &= print_check("404", status == 404, str(body))
            status, body = api_request(base, "GET", "/api/test-notify")
            all_ok &= print_check("405", status == 405, str(body))

            # #9 test-notify — OPT-IN (publica notificações reais no macOS)
            if os.environ.get("NOTIFYBOT_SMOKE_TEST_NOTIFY", "") == "1":
                status, body = api_request(base, "POST", "/api/test-notify")
                all_ok &= print_check("test-notify POST", status == 200 and body.get("ok") is True
                                      and body.get("posted") == 3, str(body))
            else:
                print("[SKIPPED] test-notify POST — publique notificações reais com NOTIFYBOT_SMOKE_TEST_NOTIFY=1")
        finally:
            proc.terminate()
            with contextlib.suppress(subprocess.TimeoutExpired, OSError):
                proc.wait(timeout=5)

    print("SMOKE RESULT: %s" % ("PASSED" if all_ok else "FAILED"))
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())