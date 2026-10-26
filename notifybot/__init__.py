"""notifybot — macOS Notification Bot implementation package.

Modules (Phase 2 layout, per PLAN-PHASE2.md):
- parser:     binary plist decoding, CoreData timestamps, binary UUID helpers
- reader:     Notification Center db2 snapshot reader
- state:      watermark/UUID deduplication state manager
- classifier: LLM (Ollama) notification classifier with tolerant JSON parsing
- router:     deterministic rules + LLM action router (Router v2)
- actions:    action dispatcher (log, telegram, command, registry actions)
- finance:    bank-notification transaction curator + SQLite ledger
- report:     SVG chart engine + finance HTML/markdown reporting
- api:        local JSON API server for the desktop UI (bot.py api, loopback only)
- cli:        CLI entrypoint logic (bot.py is the thin executable)
"""

__version__ = "2.0.0"