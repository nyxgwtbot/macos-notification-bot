"""CLI entrypoint logic for the macOS Notification Bot (bot.py forwards here)."""

import argparse
import json
import logging
import os
import signal
import subprocess
import sys
import time
from typing import List

from notifybot.actions import ActionDispatcher
from notifybot.classifier import LLMClassifier
from notifybot.reader import NotificationReader
from notifybot.router import RuleRouter
from notifybot.state import StateManager

logger = logging.getLogger("notification_bot")


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
        logger.error("Falha ao ler notificações: %s", e)
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
        # Router v2: LLM-based action routing for notifications that match no
        # deterministic rule. min_confidence below the threshold falls back to "log".
        "router": {
            "min_confidence": 0.5,
            "timeout": 15.0,
        },
        # Extensible action registry. "builtin" ids are handled natively
        # (log, telegram, finance); "command" actions run config command
        # with the event JSON in argv[1].
        "actions": [
            {
                "id": "log",
                "match_hint": (
                    "Ação padrão: apenas registrar a notificação no histórico, "
                    "sem nenhuma consequência externa."
                ),
                "type": "builtin",
                "enabled": True,
                "config": {},
            },
            {
                "id": "telegram",
                "match_hint": (
                    "Enviar alerta pessoal no Telegram: mensagens diretas de "
                    "contatos, urgências altas e avisos importantes que merecem "
                    "atenção imediata."
                ),
                "type": "builtin",
                "enabled": True,
                "config": {},
            },
            {
                "id": "finance",
                "match_hint": (
                    "Notificações bancárias e de finanças pessoais para curadoria "
                    "de gastos: compras aprovadas, Pix enviado ou recebido, "
                    "pagamentos, débitos, faturas, estornos e qualquer notificação "
                    "com valores em R$."
                ),
                "type": "builtin",
                "enabled": True,
                "config": {},
            },
            {
                "id": "example_command",
                "match_hint": (
                    "Exemplo de ação externa: executar um programa local com o "
                    "payload da notificação em argv[1]. Desabilitada por padrão."
                ),
                "type": "command",
                "enabled": False,
                "config": {"command": "/path/to/prog"},
            },
        ],
        # Deterministic rules evaluated FIRST (overrides). No catch-all rule:
        # unmatched notifications flow into the LLM router (Router v2).
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
        ],
        # Finance plugin: extracts bank-notification transactions into a local
        # SQLite ledger. bank_apps empty = ingest from every app that the
        # router decides is finance-related.
        "finance": {
            "enabled": True,
            "ledger_db": "finance.db",
            "bank_apps": [],
            "telegram_summary": False,
            "categories": [
                "alimentacao",
                "transporte",
                "compras",
                "saude",
                "lazer",
                "servicos",
                "assinaturas",
                "outros",
            ],
        },
    }

    if os.path.exists(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                loaded = json.load(f)
                defaults.update(loaded)
        except Exception as e:
            logger.warning("Não foi possível ler a configuração %s: %s; usando padrões", config_path, e)
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


def main() -> int:
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
        return 0

    reader = NotificationReader(db_path=args.db_path)

    if args.dump is not None:
        dump_notifications(reader, count=args.dump)
        return 0

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
        return 0

    # Daemon mode
    running = True

    def _handle_exit(signum, frame):
        nonlocal running
        running = False
        logger.info("Recebido sinal %d; encerrando de forma limpa...", signum)

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
            logger.error("Erro no loop de polling do daemon: %s", e)

    state_mgr.save()
    logger.info("Bot daemon encerrado.")
    return 0