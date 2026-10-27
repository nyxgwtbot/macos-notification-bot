"""In-app agent tools layer (`app.data`, `dashboard.create`, `server.serve`,
`notify.test`) and the artifacts sub-service registry.

This module owns:

- the shared agent constants block (ports, budgets, caps) imported by
  ``notifybot.agent``;
- the four tool implementations, backed by the EXISTING ``BotApiRuntime``
  handlers in ``notifybot/api.py`` (zero duplicated query logic);
- ``ArtifactServerRegistry`` — one stdlib ``ThreadingHTTPServer`` bound to
  loopback:4770 that serves ONLY ``<config_dir>/artifacts`` with a JSON
  health probe, so generated dashboards can be rendered in the desktop
  iframe.

Implemented in step 2 (skeleton in step 1).
"""

ARTIFACTS_PORT = 4770
MAX_TOOL_TURNS = 6
TURN_BUDGET_S = 90.0
TOOL_RESULT_MAX_BYTES = 4096
SESSION_CONTEXT_MESSAGES = 8
ARTIFACT_HTML_MAX_BYTES = 512_000
ARTIFACT_MAX_FILES = 200