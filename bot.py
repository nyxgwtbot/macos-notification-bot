#!/usr/bin/env python3
"""macOS Notification Bot — thin CLI entrypoint.

Implementation lives in the `notifybot` package (reader, parser, state,
classifier, router, actions, finance, report, cli). This file only forwards
to notifybot.cli.main so `python3 bot.py ...` keeps working exactly as before.

Reads macOS notifications from the Notification Center SQLite database (db2),
classifies them using local Ollama, routes actions (rules first, LLM router
otherwise), and hosts the finance tracking plugin (`bot.py report`).
"""

import sys

from notifybot.cli import main

if __name__ == "__main__":
    sys.exit(main() or 0)