"""Action dispatcher: executes routed actions and writes the event JSONL log."""

import datetime
import json
import logging
import os
import subprocess
import sys
import urllib.request
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("notification_bot")


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


def send_telegram_text(token: str, chat_id: str, text: str, parse_mode: Optional[str] = None) -> str:
    """Send a text message via the Telegram Bot API. Returns a status string.

    Missing credentials -> "skipped" (never an error), per Phase 1 correction.
    """
    if not token or not chat_id:
        logger.info("Ação do Telegram IGNORADA (token ou chat_id ausentes)")
        return "skipped"

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": text}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    try:
        req_data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
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
        logger.warning("Falha no envio via Telegram: %s", e)
        return f"error: {e}"


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
        logger.info("Bot iniciado no modo '%s' (watermark=%d)", mode, watermark)

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
                "Comando '%s' saiu com código %d. stderr: %s",
                cmd_path,
                proc.returncode,
                proc.stderr.strip(),
            )
            return f"exit_{proc.returncode}"
        except subprocess.TimeoutExpired:
            logger.error("Comando '%s' excedeu o tempo limite de 30s", cmd_path)
            return "timeout"
        except Exception as e:
            logger.error("Erro na execução do comando '%s': %s", cmd_path, e)
            return f"error: {e}"

    def send_telegram(self, notif: dict, classification: dict) -> str:
        """Send notification message via Telegram Bot API (Markdown formatting)."""
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

        return send_telegram_text(
            self.telegram_token, self.telegram_chat_id, message_text, parse_mode="Markdown"
        )

    def dispatch(
        self,
        actions: List[str],
        notif: dict,
        classification: dict,
        rule_name: str = "none",
        layer: Optional[str] = None,
        confidence: Optional[float] = None,
        router_payload: Optional[dict] = None,
    ) -> Dict[str, str]:
        """Dispatch actions according to the routed decision.

        layer: "rule" | "llm_router" | "fallback_in_fallback" (Router v2).
        confidence / router_payload carry LLM router decision metadata.
        """
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
            "layer": layer,
            "confidence": confidence if confidence is not None else None,
            "router_payload": dict(router_payload or {}),
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
            f"[{now_iso}] [CAMADA: {layer or 'n/a'}] [REGRA: {rule_name}] "
            f"[AÇÕES: {','.join(actions)}] "
            f"{notif.get('app', 'app')} - {notif.get('title', '')}: {notif.get('body', '')} "
            f"(Resumo: {classification.get('summary', '')})"
        )
        print(readable_line, file=sys.stderr)

        # Write to log_file
        self.log_event(event)
        return results