# Connectors: osascript argv-binding decision + TCC consent attribution

Connectors v1 (`notifybot/connectors.py`, tool `native.call`) — the two
decisions worth compounding:

## 1. Argv-binding as THE injection defense

Approved property: every model-supplied string travels as a positional argv
item to a *frozen constant* AppleScript source —
`subprocess.run(["osascript", "-e", SCRIPT_CONST, *argv])`. The script's only
input channel is `on run argv`; user bytes never touch script source (no
f-strings, no shell, no `run script` of dynamic text). This is strictly
stronger than heredoc/stdin script feeding: there is nothing to escape.
Practical consequences that made it work:

- List outputs join in-script with `ASCII character 31` (unit separator) and
  Python splits on `_DELIM = "\x1f"` — comma-splitting would corrupt names
  containing commas and invite quoting bugs.
- AppleScript dates are locale-dependent as STRINGS (`date "2026-10-28"`);
  `add {due}` passes year/month/day as integer argv and builds the date
  component-wise in-script (`set day to 1` FIRST, then year/month/day —
  avoids Feb-29 rollover).
- All `tell application` blocks address apps by BUNDLE ID
  (`com.apple.reminders`, `com.apple.Notes`, `com.apple.systemevents`),
  surviving pt-BR localization ("Lembretes").
- The test battery (tests/test_agent_connectors.py :: TestInjectionBattery)
  asserts payload-in-argv and payload-NOT-in-script-source for quote/escape/
  newline/`do shell script` concatenation payloads, plus
  no-`do shell script`-in-ANY-script-source. Copy that pattern for any new
  AppleScript surface.
- Flagged follow-up (not v1 scope): `notifybot/cli.py` `trigger_test_notifications`
  still interpolates into script source and should adopt the same argv pattern.

## 2. TCC consent attribution: probes must originate from the app

A probe timed out classifies `needs_consent` because the common cause is a
HIDDEN/hanging TCC dialog. Attribution gotcha: macOS TCC attributes Automation
consent to the responsible parent process — the Electron-spawned `bot.py api`
child may NOT see (or grant) the same prompt a Terminal-launched run sees.
Therefore: probes fire from the Settings button (the app itself), copy says
"approve it while this app is focused", and `system` stays ungated (System
Events/`front_app` can still prompt under the Electron child even when clean
from Terminal — the action-level timeout mapping degrades it gracefully).

## Sandbox/test facts (reusable)

- Runner seam: `ConnectorManager(config_dir, runner=fake)` — inject fakes in
  tests; env `NOTIFYBOT_CONNECTORS_MOCK=1` swaps in an in-memory world
  (per-manager, so tests get fresh state per instance) that still exercises
  the REAL validation/consent gate/argv-build paths.
- `_write_atomic` does NOT mkdir parent dirs — the state store must
  `os.makedirs(..., exist_ok=True)` before the first write (bit me once).
- Smoke needs BOTH mocks (`NOTIFYBOT_SMOKE_AGENT_MOCK=1` +
  `NOTIFYBOT_CONNECTORS_MOCK=1`) AND a pre-turn probe of `reminders`, since
  the consent gate runs BEFORE the mock runner (correct behavior).
- `python3 bot.py api --port 0` prints the REQUESTED port (0), not the bound
  one — sandboxed curl checks need a fixed free port.