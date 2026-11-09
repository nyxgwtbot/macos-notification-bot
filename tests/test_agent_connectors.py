#!/usr/bin/env python3
"""Unit tests for the connectors layer (native.call) — module, tool dispatch,
API rows 15/16, and the agent-loop integration smoke.

Mirrors tests/test_agent.py conventions: tempdir runtime, ApiServerFixture on
port 0, ScriptedLLM transcripts. The osascript runner is ALWAYS a fake (or the
ConnectorManager mock world) — no osascript execution here, and tests never
bind 4770/4769 (fixtures use ephemeral ports / tempdirs only).
"""

import json
import os
import subprocess
import tempfile
import threading
import unittest

from tests.test_api import ApiServerFixture, valid_config_payload
from notifybot.agent_tools import ArtifactServerRegistry, ToolRegistry
from notifybot.api import BotApiRuntime
from notifybot.connectors import (
    BODY_MAX_BYTES,
    CONNECTOR_DOMAINS,
    ConnectorError,
    ConnectorManager,
    HINT_GRANTED,
    HINT_SYSTEM,
    HINT_UNKNOWN,
    LIST_LIMIT_DEFAULT,
    LIST_LIMIT_MAX,
    NOTES_CREATE,
    NOTES_READ,
    NOTES_SEARCH,
    NO_ACCOUNT_MARKER,
    PROBE_SCRIPTS,
    READ_MAX_BYTES,
    SCRIPTS,
    ProbeBusy,
    _DELIM,
)
from notifybot.agent import run_turn


class Outcome:
    """Fake CompletedProcess (returncode/stdout/stderr subset)."""

    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class RecordingRunner:
    """Scripted fake runner: maps frozen script constants to canned outcomes
    and RECORDS every invocation for argv-layout assertions."""

    def __init__(self):
        self.outcomes = {}
        self.calls = []
        self.timeout_seen = None
        self.stdin_values = []  # per-call stdin page (shortcuts -x seam)

    def register(self, script, outcome):
        self.outcomes[script] = outcome

    def __call__(self, cmd, timeout, stdin=None):
        self.calls.append(list(cmd))
        self.timeout_seen = timeout
        self.stdin_values.append(stdin)
        script = cmd[2] if len(cmd) > 2 else ""
        for key, outcome in self.outcomes.items():
            if script == key:
                return outcome
        raise AssertionError("unexpected script handed to fake runner: %r" % script[:60])

    def last_argv(self):
        return self.calls[-1][3:] if self.calls else None

    def last_stdin(self):
        return self.stdin_values[-1] if self.stdin_values else None


def make_registry_runtime(tmpdir):
    """BotApiRuntime against a tempdir config + port-0 artifacts registry,
    with a 2-arg ToolRegistry (connector manager built lazily on demand)."""
    config_path = os.path.join(tmpdir, "config.json")
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(valid_config_payload(), f)
    runtime = BotApiRuntime(config_path=config_path)
    runtime.agent_registry = ArtifactServerRegistry(runtime.artifacts_dir(), port=0)
    runtime.tool_registry = ToolRegistry(runtime, runtime.agent_registry)
    return runtime


# ---- tool dispatch + registry wiring ----------------------------------------------


class TestNativeCallDispatch(unittest.TestCase):
    """native.call envelope validation + ConnectorError -> error result mapping."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.runtime = make_registry_runtime(self.tmp.name)
        self.addCleanup(self.runtime.agent_registry.stop)
        self.registry = self.runtime.tool_registry

    def test_unknown_tool_list_includes_native_call(self):
        dispatched = self.registry.dispatch("does.not", {})
        self.assertIn("unknown tool", dispatched["error"])
        self.assertIn("native.call", dispatched["error"])

    def test_envelope_validation_errors(self):
        # envelope shape errors keep the "native.call requires" prefix…
        for args in ({}, {"domain": "reminders"},
                     {"domain": "", "action": "add"}, {"domain": 5, "action": "add"}):
            dispatched = self.registry.dispatch("native.call", args)
            self.assertIn("native.call requires", dispatched["error"], args)
        # …while a non-dict params gets its own message.
        dispatched = self.registry.dispatch(
            "native.call", {"domain": "reminders", "action": "add", "params": "nope"}
        )
        self.assertIn("must be a JSON object", dispatched["error"])
        # a well-formed envelope with missing params falls through to the
        # manager's English validation error.
        dispatched = self.registry.dispatch("native.call", {"domain": "reminders", "action": "add"})
        self.assertIn("'list' is required", dispatched["error"])

    def test_lazy_connector_manager_builds_from_runtime_config_dir(self):
        self.assertIsNone(self.registry.connector_manager)
        # ungated domain, invalid open target: validation error (English) and
        # the lazy build ran from runtime.config_dir.
        dispatched = self.registry.dispatch(
            "native.call", {"domain": "system", "action": "open", "params": {"target": "-w"}}
        )
        self.assertIn("invalid open target", dispatched["error"])
        self.assertIsNotNone(self.registry.connector_manager)
        self.assertEqual(self.registry.connector_manager.config_dir, self.runtime.config_dir)

    def test_injected_connector_manager_used_directly(self):
        from notifybot.connectors import ConnectorManager

        injected = ConnectorManager(self.tmp.name, runner=lambda cmd, timeout, stdin=None: Outcome(0, "Finder", ""))
        self.registry.connector_manager = injected
        dispatched = self.registry.dispatch("native.call", {"domain": "system", "action": "front_app", "params": {}})
        self.assertEqual(dispatched, {"app": "Finder"})

    def test_connector_error_maps_to_tool_error_result(self):
        from notifybot.connectors import ConnectorManager

        # gate error (PT-BR) must surface VERBATIM as the tool error result.
        self.registry.connector_manager = ConnectorManager(
            self.tmp.name, runner=lambda cmd, timeout, stdin=None: Outcome(0, "", "")
        )
        dispatched = self.registry.dispatch(
            "native.call",
            {"domain": "reminders", "action": "add", "params": {"list": "L", "title": "t"}},
        )
        self.assertIn("error", dispatched)
        self.assertEqual(
            dispatched["error"],
            "Reminders ainda não está conectado — conecte reminders em Settings › Connectors.",
        )


# ---- module-level: runner seam + normalization --------------------------------------


class TestRunnerSeamActions(unittest.TestCase):
    """Per-action argv layout, \\x1f parsing, caps, and system actions."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.runner = RecordingRunner()
        self.manager = ConnectorManager(self.tmp.name, runner=self.runner)

    def test_list_lists_argv_shape_and_delimiter_parsing(self):
        self.manager.state.record("reminders", "granted")
        self.runner.register(
            SCRIPTS[("reminders", "list_lists")],
            Outcome(0, _DELIM.join(["Pão, de festas", "Trabalho", "", "Compras"]), ""),
        )
        result = self.manager.run_action("reminders", "list_lists", {})
        self.assertEqual(result, {"lists": ["Pão, de festas", "Trabalho", "Compras"]})
        cmd = self.runner.calls[-1]
        self.assertEqual(cmd[:3], ["osascript", "-e", SCRIPTS[("reminders", "list_lists")]])
        self.assertEqual(cmd[3:], [])
        self.assertEqual(self.runner.timeout_seen, 8.0)

    def test_list_reminders_limit_default_cap_and_argv(self):
        self.manager.state.record("reminders", "granted")
        self.runner.register(
            SCRIPTS[("reminders", "list_reminders")],
            Outcome(0, _DELIM.join(["Pão", "Leite"]), ""),
        )
        cases = [
            ({"list": "Compras"}, ["Compras", str(LIST_LIMIT_DEFAULT)]),
            ({"list": "Compras", "limit": 3}, ["Compras", "3"]),
            ({"list": "Compras", "limit": 500}, ["Compras", str(LIST_LIMIT_MAX)]),
            ({"list": "Compras", "limit": "7"}, ["Compras", "7"]),
        ]
        for params, expected_argv in cases:
            result = self.manager.run_action("reminders", "list_reminders", params)
            self.assertEqual(result, {"list": "Compras", "reminders": ["Pão", "Leite"]})
            self.assertEqual(self.runner.last_argv(), expected_argv)
        for bad in ({"list": "Compras", "limit": 0}, {"list": "Compras", "limit": -1},
                    {"list": "Compras", "limit": 2.5}, {"list": "Compras", "limit": True},
                    {"limit": 3}, {"list": ""}):
            with self.assertRaises(ConnectorError):
                self.manager.run_action("reminders", "list_reminders", bad)

    def test_add_builds_locale_proof_date_argv(self):
        self.manager.state.record("reminders", "granted")
        self.runner.register(SCRIPTS[("reminders", "add")], Outcome(0, "ok", ""))
        result = self.manager.run_action(
            "reminders", "add", {"list": "Compras", "title": "Pão", "due": "2026-11-05"}
        )
        self.assertEqual(result, {"ok": True, "list": "Compras", "title": "Pão", "due": "2026-11-05"})
        self.assertEqual(self.runner.last_argv(), ["Compras", "Pão", "2026", "11", "05"])
        result2 = self.manager.run_action("reminders", "add", {"list": "Compras", "title": "Sem data"})
        self.assertEqual(result2, {"ok": True, "list": "Compras", "title": "Sem data"})
        self.assertEqual(self.runner.last_argv(), ["Compras", "Sem data", "", "", ""])
        for bad_due in ("2026-13-01", "01-01-2026", "2026-1-1", "not-a-date"):
            with self.assertRaises(ConnectorError):
                self.manager.run_action("reminders", "add", {"list": "C", "title": "t", "due": bad_due})

    def test_complete_maps_not_found_and_argv(self):
        self.manager.state.record("reminders", "granted")
        self.runner.register(SCRIPTS[("reminders", "complete")], Outcome(0, "not_found", ""))
        with self.assertRaises(ConnectorError) as ctx:
            self.manager.run_action("reminders", "complete", {"list": "Compras", "title": "Pão"})
        self.assertIn("Lembrete", str(ctx.exception))
        self.assertIn("não encontrado", str(ctx.exception))
        self.assertEqual(self.runner.last_argv(), ["Compras", "Pão"])
        self.runner.register(
            SCRIPTS[("reminders", "complete")], Outcome(0, "ok" + _DELIM + "Pão de forma", "")
        )
        result = self.manager.run_action(
            "reminders", "complete", {"list": "Compras", "id": "pão de forma"}
        )
        self.assertEqual(result, {"ok": True, "completed": "Pão de forma"})
        self.assertEqual(self.runner.last_argv(), ["Compras", "pão de forma"])
        with self.assertRaises(ConnectorError):
            self.manager.run_action("reminders", "complete", {"list": "C", "title": "a", "id": "b"})
        with self.assertRaises(ConnectorError):
            self.manager.run_action("reminders", "complete", {"list": "C"})

    def test_notes_search_read_create_caps(self):
        self.manager.state.record("notes", "granted")
        self.runner.register(
            SCRIPTS[("notes", "search")], Outcome(0, _DELIM.join(["Ideias", "Compras"]), "")
        )
        result = self.manager.run_action("notes", "search", {"query": "ideia", "limit": 5})
        self.assertEqual(result, {"notes": ["Ideias", "Compras"]})
        self.assertEqual(self.runner.last_argv(), ["ideia", "5"])

        self.runner.register(SCRIPTS[("notes", "create")], Outcome(0, "ok", ""))
        result2 = self.manager.run_action(
            "notes", "create", {"title": "Nota", "body": "b" * (BODY_MAX_BYTES + 100)}
        )
        self.assertEqual(result2, {"ok": True, "name": "Nota"})
        sent_body = self.runner.last_argv()[1]
        self.assertEqual(len(sent_body.encode("utf-8")), BODY_MAX_BYTES)  # 16 KB cap

        self.runner.register(SCRIPTS[("notes", "read")], Outcome(0, "<div>" + "r" * 20000 + "</div>", ""))
        result3 = self.manager.run_action("notes", "read", {"name": "Nota"})
        self.assertEqual(result3["name"], "Nota")
        self.assertEqual(len(result3["body"].encode("utf-8")), READ_MAX_BYTES)  # 16 KB cap

    def test_notes_scripts_anchored_to_account_1(self):
        """AppleScript targets are anchored deterministically (account 1 /
        folder 1 of account 1) — the implicit default-target surface is the
        one that hangs; the argv channel stays title/body/query only."""
        self.assertIn("folder 1 of account 1", NOTES_CREATE)
        self.assertIn("count of accounts", NOTES_CREATE)
        self.assertIn("notes of account 1", NOTES_SEARCH)
        self.assertIn("notes of account 1", NOTES_READ)
        self.assertIn(NO_ACCOUNT_MARKER, NOTES_CREATE)

    def test_create_argv_still_title_body_only(self):
        """The anchor is in-script ONLY: argv stays exactly [title, body] —
        no model-controlled target strings ever reach the anchor."""
        self.manager.state.record("notes", "granted")
        self.runner.register(SCRIPTS[("notes", "create")], Outcome(0, "ok", ""))
        self.manager.run_action("notes", "create", {"title": "Título", "body": "corpo"})
        self.assertEqual(self.runner.last_argv(), ["Título", "corpo"])

    def test_volume_get_set_front_app_toast(self):
        self.runner.register(SCRIPTS[("system", "volume")], Outcome(0, "37", ""))
        self.assertEqual(self.manager.run_action("system", "volume", {}), {"level": 37})
        self.runner.register(SCRIPTS[("system", "volume_set")], Outcome(0, "42", ""))
        self.assertEqual(self.manager.run_action("system", "volume", {"level": 42}), {"ok": True, "level": 42})
        self.assertEqual(self.runner.last_argv(), ["42"])
        # string levels coerce; echo the requested level back for the assert
        self.manager._runner = lambda cmd, timeout, stdin=None: Outcome(0, cmd[3], "")
        self.assertEqual(self.manager.run_action("system", "volume", {"level": "7"}), {"ok": True, "level": 7})
        self.manager._runner = self.runner
        for bad in (101, -1, "abc", True, 1.5):
            with self.assertRaises(ConnectorError):
                self.manager.run_action("system", "volume", {"level": bad})
        self.runner.register(SCRIPTS[("system", "front_app")], Outcome(0, "Safari", ""))
        self.assertEqual(self.manager.run_action("system", "front_app", {}), {"app": "Safari"})
        self.runner.register(SCRIPTS[("system", "toast")], Outcome(0, "ok", ""))
        result = self.manager.run_action("system", "toast", {"title": "Olá", "body": "Tudo certo"})
        self.assertEqual(result, {"ok": True, "title": "Olá"})
        self.assertEqual(self.runner.last_argv(), ["Olá", "Tudo certo"])

    def test_open_accept_reject_matrix(self):
        open_calls = []

        def open_runner(cmd, timeout, stdin=None):
            open_calls.append(list(cmd))
            return Outcome(0, "", "")

        self.manager._runner = open_runner
        # https/http URL -> plain open
        self.manager.run_action("system", "open", {"target": "https://example.com/x?y=1"})
        self.assertEqual(open_calls[-1], ["open", "https://example.com/x?y=1"])
        self.manager.run_action("system", "open", {"target": "http://127.0.0.1/"})
        self.assertEqual(open_calls[-1], ["open", "http://127.0.0.1/"])
        # app names -> open -a
        self.manager.run_action("system", "open", {"target": "My App"})
        self.assertEqual(open_calls[-1], ["open", "-a", "My App"])
        self.manager.run_action("system", "open", {"target": "Notes"})
        self.assertEqual(open_calls[-1], ["open", "-a", "Notes"])
        # rejected: flag-like dash prefix (CLI arg injection)
        with self.assertRaises(ConnectorError):
            self.manager.run_action("system", "open", {"target": "-Revealing"})
        # rejected: non-http schemes
        with self.assertRaises(ConnectorError):
            self.manager.run_action("system", "open", {"target": "file:///etc/passwd"})
        # rejected: shell metacharacters
        with self.assertRaises(ConnectorError):
            self.manager.run_action("system", "open", {"target": "$(touch /tmp/nope)"})
        with self.assertRaises(ConnectorError):
            self.manager.run_action("system", "open", {"target": "x" * 65})
        self.assertEqual(len(open_calls), 4)  # rejects never reached the runner


# ---- failure mapping + consent gating -----------------------------------------------


class TestFailureMappingAndConsent(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.manager = ConnectorManager(
            self.tmp.name,
            runner=lambda cmd, timeout, stdin=None: Outcome(0, "", ""),
        )

    def test_timeout_on_granted_domain_gets_retry_copy_without_connect(self):
        """A timeout on an ALREADY-granted domain is not a consent problem: the
        copy must be the retry wording, with NO Settings/Connect redirect."""
        def timeout_runner(cmd, timeout, stdin=None):
            raise subprocess.TimeoutExpired(cmd, timeout)

        self.manager._runner = timeout_runner
        self.manager.state.record("reminders", "granted")
        with self.assertRaises(ConnectorError) as ctx:
            self.manager.run_action("reminders", "add", {"list": "L", "title": "t"})
        message = str(ctx.exception)
        self.assertIn("não respondeu", message)
        self.assertIn("tente de novo", message)
        self.assertNotIn("Connect", message)
        self.assertNotIn("Settings", message)

    def test_timeout_on_ungranted_domain_keeps_settings_hint(self):
        """Ungranted-state timeouts keep the consent copy (Settings hint).
        `system` is ungated, so its unknown state reaches the timeout with the
        Settings/Connect variant."""
        def timeout_runner(cmd, timeout, stdin=None):
            raise subprocess.TimeoutExpired(cmd, timeout)

        self.manager._runner = timeout_runner
        # system row is unknown -> the "ungranted" side of the split
        self.assertEqual(self.manager.state.read()["system"]["status"], "unknown")
        with self.assertRaises(ConnectorError) as ctx:
            self.manager.run_action("system", "front_app", {})
        message = str(ctx.exception)
        self.assertIn("consentimento", message)
        self.assertIn("Settings › Connectors", message)
        self.assertNotIn("reexecuto", message)

    def test_deny_maps_to_ptbr_error_and_persists_denied(self):
        def deny_runner(cmd, timeout, stdin=None):
            return Outcome(1, "", "script error: Not authorized to send Apple events (-1743)")

        self.manager._runner = deny_runner
        self.manager.state.record("notes", "granted")
        with self.assertRaises(ConnectorError) as ctx:
            self.manager.run_action("notes", "read", {"name": "X"})
        self.assertIn("-1743", str(ctx.exception))
        self.assertIn("Settings › Connectors", str(ctx.exception))
        self.assertEqual(self.manager.state.read()["notes"]["status"], "denied")

    def test_notes_create_no_account_marker_maps_to_ptbr(self):
        """NOTES_CREATE's structured in-script error (no Notes account) maps to
        the PT-BR account-setup tool error, not the generic script-error copy."""
        self.manager.state.record("notes", "granted")
        self.manager._runner = lambda cmd, timeout, stdin=None: Outcome(
            1, "", "execution error: Notes got an error: %s (-2700)" % NO_ACCOUNT_MARKER
        )
        with self.assertRaises(ConnectorError) as ctx:
            self.manager.run_action("notes", "create", {"title": "T"})
        message = str(ctx.exception)
        self.assertIn("configure uma conta no Notas primeiro", message)
        self.assertNotIn("action failed", message)

    def test_other_stderr_maps_to_english_trimmed_error(self):
        def boom_runner(cmd, timeout, stdin=None):
            return Outcome(3, "", "script error: Can't get list " + "x" * 500)

        self.manager._runner = boom_runner
        with self.assertRaises(ConnectorError) as ctx:
            self.manager.run_action("system", "front_app", {})  # system is ungated
        message = str(ctx.exception)
        self.assertIn("System action failed (exit 3)", message)
        self.assertLessEqual(len(message), 260)  # stderr trimmed server-side

    def test_success_self_heals_concurrent_state_downgrade(self):
        """Success on a gated domain bumps the state file back to granted
        (heals a downgrade that raced between the consent gate and the run)."""
        self.manager.state.record("reminders", "granted")

        def sneaky_runner(cmd, timeout, stdin=None):
            # simulate a concurrent downgrade happening mid-run
            self.manager.state.record("reminders", "needs_consent")
            return Outcome(0, "ok", "")

        self.manager._runner = sneaky_runner
        self.manager.state.record("reminders", "granted")
        result = self.manager.run_action("reminders", "add", {"list": "L", "title": "t"})
        self.assertEqual(result["ok"], True)
        self.assertEqual(self.manager.state.read()["reminders"]["status"], "granted")

    def test_unknown_state_gates_and_runner_never_invoked(self):
        runner = RecordingRunner()
        runner.register(SCRIPTS[("reminders", "add")], Outcome(0, "ok", ""))
        manager = ConnectorManager(self.tmp.name, runner=runner)
        for status in ("unknown", "denied", "needs_consent"):
            manager.state.record("reminders", status)
            self.assertEqual(runner.calls, [])
            with self.assertRaises(ConnectorError) as ctx:
                manager.run_action("reminders", "add", {"list": "L", "title": "t"})
            self.assertIn("Settings › Connectors", str(ctx.exception))
            self.assertEqual(runner.calls, [])  # runner never invoked on the gate

    def test_gated_notes_requires_consent_too(self):
        runner = RecordingRunner()
        runner.register(SCRIPTS[("notes", "search")], Outcome(0, "a", ""))
        manager = ConnectorManager(self.tmp.name, runner=runner)
        with self.assertRaises(ConnectorError) as ctx:
            manager.run_action("notes", "search", {"query": "x"})
        self.assertIn("Notes", str(ctx.exception))
        self.assertEqual(runner.calls, [])

    def test_whitelist_unknown_domain_action_english(self):
        with self.assertRaises(ConnectorError) as ctx:
            self.manager.run_action("nope", "add", {})
        self.assertIn("unknown domain", str(ctx.exception))
        with self.assertRaises(ConnectorError) as ctx:
            self.manager.run_action("reminders", "nope", {})
        self.assertIn("unknown action", str(ctx.exception))
        with self.assertRaises(ConnectorError):
            self.manager.run_action("reminders", "add", ["not", "a", "dict"])

    def test_probe_classification_and_persistence(self):
        granted_runner = RecordingRunner()
        granted_runner.register(PROBE_SCRIPTS["reminders"], Outcome(0, "2", ""))
        manager = ConnectorManager(self.tmp.name, runner=granted_runner)
        self.assertEqual(manager.probe("reminders")["status"], "granted")
        self.assertEqual(manager.state.read()["reminders"]["status"], "granted")

        def timeout_runner(cmd, timeout, stdin=None):
            raise subprocess.TimeoutExpired(cmd, timeout)

        manager._runner = timeout_runner
        self.assertEqual(manager.probe("reminders")["status"], "needs_consent")
        self.assertEqual(manager.state.read()["reminders"]["status"], "needs_consent")

        manager._runner = lambda cmd, timeout, stdin=None: Outcome(
            1, "", "Not authorized to send Apple events (-1743)"
        )
        self.assertEqual(manager.probe("reminders")["status"], "denied")
        self.assertEqual(manager.state.read()["reminders"]["status"], "denied")


# ---- injection battery ---------------------------------------------------------------


class TestInjectionBattery(unittest.TestCase):
    """Model-controlled strings are argv-only: never in script source."""

    PAYLOADS = [
        'never"quoted',
        'esc\\"aped',
        "two\nlines\nand\ttab",
        '" & (do shell script "touch /tmp/pwned") & "',
        "' ; do shell script \"touch /tmp/pwned\" ; '",
        "$(touch /tmp/pwned)",
        "`touch /tmp/pwned`",
        "unicode—accents—ção",
    ]

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.runner = RecordingRunner()
        self.runner.register(SCRIPTS[("reminders", "add")], Outcome(0, "ok", ""))
        self.runner.register(SCRIPTS[("notes", "create")], Outcome(0, "ok", ""))
        self.runner.register(SCRIPTS[("system", "toast")], Outcome(0, "ok", ""))
        self.manager = ConnectorManager(self.tmp.name, runner=self.runner)
        self.manager.state.record("reminders", "granted")
        self.manager.state.record("notes", "granted")
        self.pwned = os.path.join(self.tmp.name, "pwned-marker")

    def test_add_title_payloads_arrive_verbatim_via_argv_only(self):
        for payload in self.PAYLOADS:
            self.manager.run_action("reminders", "add", {"list": "L", "title": payload})
            cmd = self.runner.calls[-1]
            script_source = cmd[2]
            argv = cmd[3:]
            self.assertEqual(argv[:2], ["L", payload], payload)  # verbatim delivery
            self.assertEqual(script_source, SCRIPTS[("reminders", "add")])
            self.assertNotIn(payload, script_source, payload)  # absent from source
            self.assertNotIn("do shell script", script_source)
            self.assertNotIn("$(", script_source)
        for cmd in self.runner.calls:  # shape holds for EVERY payload
            self.assertEqual(cmd[:2], ["osascript", "-e"])

    def test_notes_body_and_toast_payloads_argv_only(self):
        payload = '" & (do shell script "touch /tmp/pwned") & "'
        self.manager.run_action("notes", "create", {"title": "T", "body": payload})
        cmd = self.runner.calls[-1]
        self.assertEqual(cmd[2], SCRIPTS[("notes", "create")])
        self.assertEqual(cmd[4], payload)
        self.assertNotIn(payload, cmd[2])
        self.manager.run_action("system", "toast", {"title": payload, "body": payload})
        cmd = self.runner.calls[-1]
        self.assertEqual(cmd[2], SCRIPTS[("system", "toast")])
        self.assertEqual(cmd[3], payload)
        self.assertEqual(cmd[4], payload)
        self.assertNotIn(payload, cmd[2])

    def test_no_pwned_file_created_without_real_osascript(self):
        self.manager.run_action(
            "reminders",
            "add",
            {"list": "L", "title": '" & (do shell script "touch %s") & "' % self.pwned},
        )
        self.assertFalse(os.path.exists(self.pwned))

    def test_script_sources_never_contain_execution_primitives(self):
        for key, source in list(SCRIPTS.items()) + list(PROBE_SCRIPTS.items()):
            self.assertNotIn("do shell script", source, key)
            self.assertNotIn("run script", source, key)
            # Input-channel discipline: every script binds model input ONLY
            # from its run handler — plain `on run argv` (FASE 0/1) or
            # declared named params (`on run {mailboxArg, ...}`, mail) — or
            # takes NO input at all (the zero-input mail.mailboxes script and
            # the mail probe are bare `tell` bodies osascript runs directly).
            self.assertTrue(
                "on run argv" in source
                or "on run {" in source
                or key in (("mail", "mailboxes"), "mail"),
                key,
            )
        # Every app script addresses the app by bundle id (pt-BR locale-proof).
        self.assertIn("com.apple.reminders", SCRIPTS[("reminders", "list_lists")])
        self.assertIn("com.apple.Notes", SCRIPTS[("notes", "search")])
        self.assertIn("com.apple.systemevents", SCRIPTS[("system", "front_app")])
        self.assertIn("com.apple.iCal", SCRIPTS[("calendar", "list_calendars")])
        self.assertIn("com.apple.Music", SCRIPTS[("music", "status")])
        self.assertIn("com.apple.mail", SCRIPTS[("mail", "unread_count")])
        self.assertIn("com.apple.mail", SCRIPTS[("mail", "mailboxes")])
        self.assertIn("com.apple.mail", SCRIPTS[("mail", "list_recent")])

    def test_delimiter_never_comma_split(self):
        self.runner.register(
            SCRIPTS[("reminders", "list_reminders")],
            Outcome(0, "Pão, de festas" + _DELIM + "Compras", ""),
        )
        self.manager.state.record("reminders", "granted")
        result = self.manager.run_action("reminders", "list_reminders", {"list": "L"})
        self.assertEqual(result["reminders"], ["Pão, de festas", "Compras"])

    def test_mock_world_add_then_list_is_genuinely_observable(self):
        """The connector mock hook (env-gated) keeps add -> list observable."""
        try:
            os.environ["NOTIFYBOT_CONNECTORS_MOCK"] = "1"
        finally:
            pass
        try:
            manager = ConnectorManager(self.tmp.name)  # env mock runner active
            manager.state.record("reminders", "granted")
            manager.run_action("reminders", "add", {"list": "Compras", "title": "Pão"})
            result = manager.run_action("reminders", "list_reminders", {"list": "Compras"})
            self.assertEqual(result["reminders"], ["Pão"])
            manager.run_action("reminders", "complete", {"list": "Compras", "title": "Pão"})
            result2 = manager.run_action("reminders", "list_reminders", {"list": "Compras"})
            self.assertEqual(result2["reminders"], [])
        finally:
            os.environ.pop("NOTIFYBOT_CONNECTORS_MOCK", None)


# ---- state store: atomic-write concurrency --------------------------------------------


class TestStateStoreConcurrency(unittest.TestCase):
    def test_concurrent_probe_and_write_leaves_state_parseable(self):
        with tempfile.TemporaryDirectory() as root:
            manager = ConnectorManager(root, runner=lambda cmd, timeout, stdin=None: Outcome(0, "0", ""))
            errors = []

            def hammer():
                try:
                    for round_index in range(30):
                        manager.state.record("reminders", "granted" if round_index % 2 == 0 else "denied")
                        manager.state.read()
                except Exception as exc:  # noqa: BLE001
                    errors.append(exc)

            threads = [threading.Thread(target=hammer) for _ in range(8)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertEqual(errors, [])
            with open(manager.state.path, "r", encoding="utf-8") as f:
                data = json.load(f)  # a torn file would raise here
            self.assertIn(data["reminders"]["status"], {"granted", "denied"})

    def test_read_missing_file_yields_unknown(self):
        with tempfile.TemporaryDirectory() as root:
            manager = ConnectorManager(root, runner=lambda cmd, timeout, stdin=None: Outcome(0, "0", ""))
            rows = manager.state.read()
        self.assertEqual(
            {domain: rows[domain]["status"] for domain in CONNECTOR_DOMAINS},
            {"reminders": "unknown", "notes": "unknown", "calendar": "unknown",
             "music": "unknown", "shortcuts": "unknown", "mail": "unknown",
             "contacts": "unknown", "messages": "unknown",
             "finder": "unknown", "quicktime": "unknown",
             "system": "unknown"},
        )

    def test_single_flight_probe_busy_then_free(self):
        with tempfile.TemporaryDirectory() as root:
            manager = ConnectorManager(root, runner=lambda cmd, timeout, stdin=None: Outcome(0, "0", ""))
            lock = manager._probe_locks["notes"]
            self.assertTrue(lock.acquire(blocking=False))
            try:
                with self.assertRaises(ProbeBusy):
                    manager.probe("notes")
            finally:
                lock.release()
            self.assertEqual(manager.probe("notes")["status"], "granted")


# ---- endpoints (rows 15/16 over real HTTP) --------------------------------------------


class TestConnectorEndpoints(unittest.TestCase):
    """#15/#16: GET shape, probe happy path, 400s, 409 single-flight."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.manager = ConnectorManager(self.tmp.name, runner=lambda cmd, timeout, stdin=None: Outcome(0, "0", ""))
        self.fixture = ApiServerFixture(valid_config_payload())
        self.addCleanup(self.fixture.close)
        # Inject the mockable manager into the booted runtime (lazy singleton slot).
        self.fixture.httpd.runtime.connector_manager = self.manager

    def test_get_connectors_shape_and_system_granted(self):
        status, body = self.fixture.request("GET", "/api/connectors")
        self.assertEqual(status, 200)
        self.assertIsInstance(body.get("pid"), int)
        self.assertEqual(body["pid"], os.getpid())
        connectors = body["connectors"]
        self.assertEqual(
            set(connectors),
            {"reminders", "notes", "calendar", "music", "shortcuts", "mail",
             "contacts", "messages", "finder", "quicktime", "system"},
        )
        for domain in ("reminders", "notes", "calendar", "music", "mail",
                       "contacts", "messages", "finder", "quicktime"):
            row = connectors[domain]
            self.assertEqual(row["status"], "unknown")
            self.assertFalse(row["granted"])
            self.assertIsNone(row["last_probe"])
            self.assertEqual(row["hint"], HINT_UNKNOWN)
        self.assertEqual(
            connectors["system"],
            {"status": "granted", "granted": True, "last_probe": None, "hint": HINT_SYSTEM},
        )
        # shortcuts row: ungated via the rows() special-case (always granted,
        # own CLI hint), even with an unknown/never-probed state store row.
        self.assertEqual(
            connectors["shortcuts"],
            {"status": "granted", "granted": True, "last_probe": None,
             "hint": HINT_SHORTCUTS},
        )

    def test_post_probe_granted_and_state_file_written(self):
        status, body = self.fixture.request("POST", "/api/connectors/probe", {"domain": "notes"})
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "granted")
        self.assertTrue(body["ok"])
        self.assertEqual(body["hint"], HINT_GRANTED)
        self.assertIsInstance(body.get("pid"), int)
        self.assertEqual(body["pid"], os.getpid())
        state_path = os.path.join(self.tmp.name, "data", "connectors_state.json")
        self.assertTrue(os.path.exists(state_path))
        with open(state_path, "r", encoding="utf-8") as f:
            stored = json.load(f)
        self.assertEqual(stored["notes"]["status"], "granted")
        _, listing = self.fixture.request("GET", "/api/connectors")
        self.assertTrue(listing["connectors"]["notes"]["granted"])

    def test_probe_unknown_domain_400_and_bad_bodies_400(self):
        for payload in ({"domain": "bogus"}, {"domain": ""}, {"domain": 5}, {}, None, [1]):
            status, body = self.fixture.request("POST", "/api/connectors/probe", payload)
            self.assertEqual(status, 400, payload)
            self.assertIn("error", body)

    def test_probe_busy_409_on_concurrent_probe(self):
        probe_done = threading.Event()

        class BlockingRunner:
            def __call__(self, cmd, timeout, stdin=None):
                probe_done.wait(timeout=5)
                return Outcome(0, "0", "")

        self.manager._runner = BlockingRunner()
        lock = self.manager._probe_locks["notes"]
        self.assertTrue(lock.acquire(blocking=False))
        try:
            status, body = self.fixture.request("POST", "/api/connectors/probe", {"domain": "notes"})
            self.assertEqual(status, 409)
            self.assertIn("already running", body["error"])
        finally:
            lock.release()
        probe_done.set()

    def test_probe_plus_write_concurrently_keeps_get_parseable(self):
        errors = []

        def hammer():
            try:
                for round_index in range(20):
                    self.manager.state.record("notes", "granted")
                    self.fixture.request("GET", "/api/connectors")
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=hammer) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])


# ---- agent-loop integration smoke ------------------------------------------------------


class FakeRemindersWorldRunner:
    """In-memory reminders world behind the runner seam: exercises run_action's
    full path (argv in, delimiter parse out) rather than the mock hook."""

    def __init__(self):
        self.lists = {"Leituras": []}
        self.calls = []

    def __call__(self, cmd, timeout, stdin=None):
        self.calls.append(list(cmd))
        script = cmd[2]
        argv = list(cmd[3:])
        if script == PROBE_SCRIPTS["reminders"]:
            return Outcome(0, "1", "")
        if script == SCRIPTS[("reminders", "add")]:
            list_name, title = argv[:2]
            self.lists.setdefault(list_name, []).append({"title": title, "completed": False})
            return Outcome(0, "ok", "")
        if script == SCRIPTS[("reminders", "list_reminders")]:
            list_name = argv[0]
            names = [
                item["title"]
                for item in self.lists.get(list_name, [])
                if not item["completed"]
            ][: int(argv[1])]
            return Outcome(0, _DELIM.join(names), "")
        raise AssertionError(cmd)


class TestAgentNativeCallSmoke(unittest.TestCase):
    """run_turn + ScriptedLLM + injected fake runner: add reminder ->
    list_reminders contains it -> PT-BR answer naming the item."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.runtime = make_registry_runtime(self.tmp.name)
        self.addCleanup(self.runtime.agent_registry.stop)
        self.world = FakeRemindersWorldRunner()
        from notifybot.connectors import ConnectorManager

        manager = ConnectorManager(self.runtime.config_dir, runner=self.world)
        self.runtime.tool_registry.connector_manager = manager
        # Consented BEFORE the turn — probes flow through the same manager.
        self.assertTrue(manager.probe("reminders")["ok"])

    def test_add_then_list_then_answer(self):
        llm_calls = []

        def script(messages):
            llm_calls.append(messages)
            step = len(llm_calls)
            if step == 1:
                return json.dumps(
                    {
                        "tool": "native.call",
                        "args": {
                            "domain": "reminders",
                            "action": "add",
                            "params": {"list": "Leituras", "title": "Ler o relatório X"},
                        },
                    }
                )
            if step == 2:
                return json.dumps(
                    {
                        "tool": "native.call",
                        "args": {
                            "domain": "reminders",
                            "action": "list_reminders",
                            "params": {"list": "Leituras"},
                        },
                    }
                )
            result = json.loads(
                [
                    line[len("tool_result:") :]
                    for line in messages[-1]["content"].splitlines()
                    if line.startswith("tool_result:")
                ][0]
            )
            assert "Ler o relatório X" in result["reminders"], result
            return json.dumps(
                {
                    "answer": "Criei o lembrete 'Ler o relatório X' na lista Leituras.",
                    "urls": [],
                }
            )

        response = run_turn(self.runtime, None, "adiciona lembrete Ler o relatório X", llm_fn=script)
        self.assertEqual([step["tool"] for step in response["turn"]], ["native.call", "native.call"])
        self.assertEqual([step["args"]["action"] for step in response["turn"]], ["add", "list_reminders"])
        self.assertEqual(response["turn"][0]["result"]["ok"], True)
        self.assertIn("Ler o relatório X", response["turn"][1]["result"]["reminders"])
        self.assertIn("Ler o relatório X", response["answer"])
        # Session JSONL persisted (user + 2x(raw, step) + answer).
        session_path = os.path.join(
            self.runtime.config_dir, "data", "agent_sessions", response["session_id"] + ".jsonl"
        )
        self.assertTrue(os.path.exists(session_path))
        roles = []
        with open(session_path, "r", encoding="utf-8") as f:
            for line in f:
                roles.append(json.loads(line).get("role"))
        # user + (raw, step) per tool call + (raw, answer) for the answer call.
        self.assertEqual(
            roles,
            ["user", "assistant_raw", "assistant", "assistant_raw", "assistant",
             "assistant_raw", "assistant"],
        )
        # The fake world holds the item (add was genuinely executed).
        self.assertEqual(self.world.lists["Leituras"][0]["title"], "Ler o relatório X")

    def test_ungranted_domain_surfaces_ptbr_error_verbatim(self):
        llm_calls = []

        def script(messages):
            llm_calls.append(messages)
            step = len(llm_calls)
            if step == 1:
                return json.dumps(
                    {
                        "tool": "native.call",
                        "args": {
                            "domain": "notes",
                            "action": "search",
                            "params": {"query": "receitas"},
                        },
                    }
                )
            error = json.loads(
                [
                    line[len("tool_error:") :]
                    for line in messages[-1]["content"].splitlines()
                    if line.startswith("tool_error:")
                ][0]
            )["error"]
            assert "Settings › Connectors" in error, error
            return json.dumps({"answer": error, "urls": []})

        response = run_turn(self.runtime, None, "procura receitas nas notas", llm_fn=script)
        self.assertEqual(response["turn"][0]["tool"], "native.call")
        self.assertIn("error", response["turn"][0])
        # PT-BR consent error relayed VERBATIM as the final answer.
        self.assertEqual(response["answer"], response["turn"][0]["error"])
        self.assertIn("conecte notes em Settings › Connectors", response["answer"])


# ---- calendar domain: dispatch + argv + normalization --------------------------------
# Imports for the calendar/music suites below: appended here (not merged into
# the top block) so this change stays a pure addition to the file.

from datetime import datetime, timedelta

from notifybot.connectors import (
    CALENDAR_DELETE_EVENT,
    CALENDAR_LIST_CALENDARS,
    DATERANGE_DEFAULT_DAYS,
    DATERANGE_MAX_DAYS,
    LIST_NAME_MAX_CHARS,
    MUSIC_STATUS,
    QUERY_MAX_CHARS,
    TITLE_MAX_CHARS,
)


class TestCalendarDispatchActions(unittest.TestCase):
    """Per-action argv layout, \\x1f triplet parsing, caps, and validation for
    the native calendar actions."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.runner = RecordingRunner()
        self.manager = ConnectorManager(self.tmp.name, runner=self.runner)

    def test_list_calendars_argv_shape_and_delimiter_parsing(self):
        self.manager.state.record("calendar", "granted")
        self.runner.register(
            SCRIPTS[("calendar", "list_calendars")],
            Outcome(0, _DELIM.join(["Pessoal", "Trabalho", "", "Foco"]), ""),
        )
        result = self.manager.run_action("calendar", "list_calendars", {})
        self.assertEqual(result, {"calendars": ["Pessoal", "Trabalho", "Foco"]})
        cmd = self.runner.calls[-1]
        self.assertEqual(cmd[:3], ["osascript", "-e", CALENDAR_LIST_CALENDARS])
        self.assertEqual(cmd[3:], [])  # argv ends after the script constant
        self.assertEqual(self.runner.timeout_seen, 8.0)

    def test_list_events_argv_layout_default_and_explicit_days_triplet_rows(self):
        self.manager.state.record("calendar", "granted")
        rows = []
        expected = []
        for index in range(4):
            start_text = "2026-05-%02dT09:00" % (index + 1)
            end_text = "2026-05-%02dT10:00" % (index + 1)
            rows += ["Evento %d" % index, start_text, end_text]
            expected.append({"summary": "Evento %d" % index, "start": start_text, "end": end_text})
        self.runner.register(SCRIPTS[("calendar", "list_events")], Outcome(0, _DELIM.join(rows), ""))
        # default days: no calendar given -> argv ["", 7]; NO "calendar" in result
        default_result = self.manager.run_action("calendar", "list_events", {})
        self.assertEqual(default_result, {"events": expected})
        self.assertEqual(self.runner.last_argv(), ["", str(DATERANGE_DEFAULT_DAYS)])
        # explicit days + calendar: echoed result, exact argv, string days coerce
        explicit = self.manager.run_action("calendar", "list_events", {"calendar": "Trabalho", "days": 3})
        self.assertEqual(explicit, {"events": expected, "calendar": "Trabalho"})
        self.assertEqual(self.runner.last_argv(), ["Trabalho", "3"])
        string_days = self.manager.run_action("calendar", "list_events", {"days": "7"})
        self.assertEqual(self.runner.last_argv(), ["", "7"])
        self.assertEqual(len(string_days["events"]), 4)  # 4 full triplets -> exactly 4 rows

    def test_days_validation_failures_all_rejected_not_clamped(self):
        self.manager.state.record("calendar", "granted")
        for bad in (0, -1, 31, 100, 2.5, True, "abc"):
            with self.assertRaises(ConnectorError) as ctx:
                self.manager.run_action("calendar", "list_events", {"days": bad})
            self.assertIn(
                "between 1 and %d" % DATERANGE_MAX_DAYS, str(ctx.exception), bad
            )
        # upper boundary is VALID: DATERANGE_MAX_DAYS passes, DATERANGE_MAX_DAYS+1
        # above already failed — a validation failure, NOT a clamp.
        self.runner.register(SCRIPTS[("calendar", "list_events")], Outcome(0, "", ""))
        result = self.manager.run_action("calendar", "list_events", {"days": DATERANGE_MAX_DAYS})
        self.assertEqual(result, {"events": []})
        self.assertEqual(self.runner.last_argv(), ["", str(DATERANGE_MAX_DAYS)])

    def test_add_event_fourteen_item_argv_layout_and_result_echo(self):
        self.manager.state.record("calendar", "granted")
        self.runner.register(SCRIPTS[("calendar", "add_event")], Outcome(0, "ok", ""))
        result = self.manager.run_action(
            "calendar",
            "add_event",
            {
                "summary": "Pão da tarde",
                "start": "2026-05-05 10:00",
                "end": "2026-05-05 11:30",
                "calendar": "Trabalho",
                "notes": "Trazer pão",
                "alert_minutes": 15,
            },
        )
        self.assertEqual(
            result,
            {"ok": True, "summary": "Pão da tarde", "start": "2026-05-05 10:00", "calendar": "Trabalho"},
        )
        self.assertEqual(self.runner.last_argv(), [
            "Pão da tarde",
            "2026", "05", "05", "10", "00",
            "2026", "05", "05", "11", "30",
            "Trabalho", "Trazer pão", "15",
        ])
        self.assertEqual(len(self.runner.last_argv()), 14)
        # all-optional-absent variant: zero-padded components, three empty tails
        result2 = self.manager.run_action(
            "calendar", "add_event",
            {"summary": "S", "start": "2026-12-31 00:05", "end": "2027-01-01 23:59"},
        )
        self.assertEqual(result2, {"ok": True, "summary": "S", "start": "2026-12-31 00:05"})
        self.assertEqual(
            self.runner.last_argv(),
            ["S", "2026", "12", "31", "00", "05", "2027", "01", "01", "23", "59", "", "", ""],
        )

    def test_add_event_datetime_alert_and_cap_validation_failures(self):
        self.manager.state.record("calendar", "granted")
        self.runner.register(SCRIPTS[("calendar", "add_event")], Outcome(0, "ok", ""))
        base = {"summary": "S", "start": "2026-05-05 10:00", "end": "2026-05-05 11:00"}
        for bad_start in ("2026-05-05", "05-05-2026 10:00", "2026-13-05 10:00",
                          "2026-05-05 24:00", "2026-05-05 10:60", "not-a-date"):
            with self.assertRaises(ConnectorError):
                self.manager.run_action("calendar", "add_event", {**base, "start": bad_start})
        for bad_alert in (0, -5, 2.5, True):
            with self.assertRaises(ConnectorError):
                self.manager.run_action("calendar", "add_event", {**base, "alert_minutes": bad_alert})
        # caps: summary bounded by LIST_NAME_MAX_CHARS (256 boundary is valid),
        # notes bounded by TITLE_MAX_CHARS.
        self.manager.run_action("calendar", "add_event", {**base, "summary": "x" * LIST_NAME_MAX_CHARS})
        with self.assertRaises(ConnectorError) as ctx:
            self.manager.run_action("calendar", "add_event", {**base, "summary": "x" * (LIST_NAME_MAX_CHARS + 1)})
        self.assertIn("limited to %d" % LIST_NAME_MAX_CHARS, str(ctx.exception))
        self.manager.run_action("calendar", "add_event", {**base, "notes": "n" * TITLE_MAX_CHARS})
        with self.assertRaises(ConnectorError):
            self.manager.run_action("calendar", "add_event", {**base, "notes": "n" * (TITLE_MAX_CHARS + 1)})

    def test_delete_event_argv_layout_and_count_semantics(self):
        self.manager.state.record("calendar", "granted")
        self.runner.register(SCRIPTS[("calendar", "delete_event")], Outcome(0, "0", ""))
        self.assertEqual(
            self.manager.run_action(
                "calendar", "delete_event",
                {"summary": "Reunião", "from": "2026-05-01", "calendar": "Trabalho"},
            ),
            {"ok": True, "deleted": 0},  # "0" is a valid count, not an error
        )
        self.assertEqual(self.runner.last_argv(), ["Reunião", "2026", "05", "01", "Trabalho"])
        self.assertEqual(
            self.manager.run_action("calendar", "delete_event", {"summary": "Compras"}),
            {"ok": True, "deleted": 0},
        )
        self.assertEqual(self.runner.last_argv(), ["Compras", "", "", "", ""])
        self.assertEqual(
            self.manager.run_action("calendar", "delete_event", {"summary": "X", "from": "2026-05-01"}),
            {"ok": True, "deleted": 0},
        )
        self.assertEqual(self.runner.last_argv(), ["X", "2026", "05", "01", ""])
        self.runner.register(SCRIPTS[("calendar", "delete_event")], Outcome(0, "2", ""))
        self.assertEqual(
            self.manager.run_action(
                "calendar", "delete_event", {"summary": "Reunião", "calendar": "Trabalho"}
            ),
            {"ok": True, "deleted": 2},
        )
        self.assertEqual(self.runner.last_argv(), ["Reunião", "", "", "", "Trabalho"])
        # non-integer stdout and out-of-range from-date both fail closed
        self.runner.register(SCRIPTS[("calendar", "delete_event")], Outcome(0, "zero", ""))
        with self.assertRaises(ConnectorError):
            self.manager.run_action("calendar", "delete_event", {"summary": "Reunião"})
        with self.assertRaises(ConnectorError):
            self.manager.run_action("calendar", "delete_event", {"summary": "Reunião", "from": "2026-13-01"})

    def test_delete_summary_payload_arrives_verbatim_argv_only(self):
        """Injection parity (TestInjectionBattery style): the model-controlled
        summary rides argv verbatim; the script source stays frozen bytes."""
        payload = '" & (do shell script "touch /tmp/pwned") & "'
        self.manager.state.record("calendar", "granted")
        self.runner.register(SCRIPTS[("calendar", "delete_event")], Outcome(0, "0", ""))
        self.manager.run_action("calendar", "delete_event", {"summary": payload})
        cmd = self.runner.calls[-1]
        self.assertEqual(cmd[3], payload)  # verbatim delivery, argv item 0
        self.assertEqual(cmd[2], CALENDAR_DELETE_EVENT)  # byte-identical script source
        self.assertNotIn(payload, cmd[2])
        self.assertNotIn("do shell script", cmd[2])


# ---- music domain: dispatch + argv + normalization ------------------------------------


class TestMusicDispatchActions(unittest.TestCase):
    """Per-action argv layout, status parsing, and validation for the native
    music actions."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.runner = RecordingRunner()
        self.manager = ConnectorManager(self.tmp.name, runner=self.runner)

    def test_status_full_parse_bare_state_and_bad_numerics(self):
        self.manager.state.record("music", "granted")
        self.runner.register(
            SCRIPTS[("music", "status")],
            Outcome(0, _DELIM.join(["playing", "Pão de forma", "Artista X", "Álbum Y", "93", "210"]), ""),
        )
        result = self.manager.run_action("music", "status", {})
        self.assertEqual(
            result,
            {"state": "playing", "name": "Pão de forma", "artist": "Artista X",
             "album": "Álbum Y", "position": 93, "duration": 210},
        )
        cmd = self.runner.calls[-1]
        self.assertEqual(cmd[:3], ["osascript", "-e", MUSIC_STATUS])
        self.assertEqual(cmd[3:], [])  # status carries no argv
        # bare state (no current track): track fields default to empty/zero
        self.runner.register(SCRIPTS[("music", "status")], Outcome(0, "paused", ""))
        self.assertEqual(
            self.manager.run_action("music", "status", {}),
            {"state": "paused", "name": "", "artist": "", "album": "", "position": 0, "duration": 0},
        )
        # non-numeric position/duration degrade to 0
        self.runner.register(
            SCRIPTS[("music", "status")],
            Outcome(0, _DELIM.join(["playing", "N", "", "", "abc", "xyz"]), ""),
        )
        self.assertEqual(
            self.manager.run_action("music", "status", {}),
            {"state": "playing", "name": "N", "artist": "", "album": "", "position": 0, "duration": 0},
        )

    def test_stateless_verbs_ok_with_empty_argv(self):
        self.manager.state.record("music", "granted")
        for action in ("play", "pause", "next", "previous"):
            self.runner.register(SCRIPTS[("music", action)], Outcome(0, "ok", ""))
            self.assertEqual(self.manager.run_action("music", action, {}), {"ok": True})
            cmd = self.runner.calls[-1]
            self.assertEqual(cmd[:3], ["osascript", "-e", SCRIPTS[("music", action)]])
            self.assertEqual(cmd[3:], [])

    def test_play_playlist_ok_not_found_and_cap(self):
        self.manager.state.record("music", "granted")
        self.runner.register(SCRIPTS[("music", "play_playlist")], Outcome(0, "ok", ""))
        result = self.manager.run_action("music", "play_playlist", {"name": "Treino"})
        self.assertEqual(result, {"ok": True, "playlist": "Treino"})
        self.assertEqual(self.runner.last_argv(), ["Treino"])
        self.runner.register(SCRIPTS[("music", "play_playlist")], Outcome(0, "not_found", ""))
        with self.assertRaises(ConnectorError) as ctx:
            self.manager.run_action("music", "play_playlist", {"name": "Treino"})
        self.assertEqual(
            str(ctx.exception), "Playlist 'Treino' não encontrada (verifique o nome exato)."
        )
        self.assertEqual(self.runner.last_argv(), ["Treino"])  # argv recorded even on normalize failure
        with self.assertRaises(ConnectorError):
            self.manager.run_action("music", "play_playlist", {"name": "a" * (QUERY_MAX_CHARS + 1)})

    def test_search_store_argv_and_triplet_dicts(self):
        self.manager.state.record("music", "granted")
        self.runner.register(
            SCRIPTS[("music", "search_store")],
            Outcome(0, _DELIM.join(["Track A", "Artist A", "Album A", "Track B", "Artist B", "Album B"]), ""),
        )
        result = self.manager.run_action("music", "search_store", {"query": "pão de forma"})
        self.assertEqual(
            result,
            {"results": [
                {"name": "Track A", "artist": "Artist A", "album": "Album A"},
                {"name": "Track B", "artist": "Artist B", "album": "Album B"},
            ]},
        )
        self.assertEqual(self.runner.last_argv(), ["pão de forma", "5"])

    def test_volume_get_set_and_invalid_levels(self):
        self.manager.state.record("music", "granted")
        self.runner.register(SCRIPTS[("music", "volume")], Outcome(0, "50", ""))
        self.assertEqual(self.manager.run_action("music", "volume", {}), {"level": 50})
        self.assertEqual(self.runner.last_argv(), [])
        self.runner.register(SCRIPTS[("music", "volume_set")], Outcome(0, "70", ""))
        self.assertEqual(
            self.manager.run_action("music", "volume", {"level": 70}), {"ok": True, "level": 70}
        )
        self.assertEqual(self.runner.last_argv(), ["70"])
        # 0 is a VALID set level (falsy must not bounce into the get path)
        self.runner.register(SCRIPTS[("music", "volume_set")], Outcome(0, "0", ""))
        self.assertEqual(
            self.manager.run_action("music", "volume", {"level": 0}), {"ok": True, "level": 0}
        )
        self.assertEqual(self.runner.last_argv(), ["0"])
        for bad in (True, -1, 101, "abc", 1.5):
            with self.assertRaises(ConnectorError):
                self.manager.run_action("music", "volume", {"level": bad})


# ---- gating + probe + mock world (calendar + music) ------------------------------------


class TestCalendarMusicGatingProbeAndMockWorld(unittest.TestCase):
    """Consent gating (calendar/music are gated), probe grant flow, and the
    env-gated mock world observability."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_ungranted_gates_never_reach_runner_then_probe_grants_and_actions_run(self):
        runner = RecordingRunner()
        runner.register(PROBE_SCRIPTS["calendar"], Outcome(0, "2", ""))
        runner.register(PROBE_SCRIPTS["music"], Outcome(0, "true", ""))
        runner.register(
            SCRIPTS[("calendar", "list_calendars")],
            Outcome(0, _DELIM.join(["Pessoal", "Trabalho"]), ""),
        )
        runner.register(SCRIPTS[("music", "status")], Outcome(0, "paused", ""))
        manager = ConnectorManager(self.tmp.name, runner=runner)
        for domain, action, needle in (
            ("calendar", "list_calendars", "Calendar ainda não está conectado"),
            ("music", "status", "Music ainda não está conectado"),
        ):
            for status in ("unknown", "denied", "needs_consent"):
                manager.state.record(domain, status)
                self.assertEqual(runner.calls, [])
                with self.assertRaises(ConnectorError) as ctx:
                    manager.run_action(domain, action, {})
                self.assertIn(needle, str(ctx.exception))
                self.assertIn("Settings › Connectors", str(ctx.exception))
                self.assertEqual(runner.calls, [])  # runner never invoked on the gate
        # probe flow: granted status persists, then the action really runs
        self.assertEqual(manager.probe("calendar")["status"], "granted")
        self.assertEqual(manager.state.read()["calendar"]["status"], "granted")
        self.assertEqual(
            manager.run_action("calendar", "list_calendars", {}),
            {"calendars": ["Pessoal", "Trabalho"]},
        )
        self.assertEqual(manager.probe("music")["status"], "granted")
        self.assertEqual(manager.state.read()["music"]["status"], "granted")
        self.assertEqual(
            manager.run_action("music", "status", {}),
            {"state": "paused", "name": "", "artist": "", "album": "", "position": 0, "duration": 0},
        )

    def test_probe_scripts_registration_sanity(self):
        """Probe coverage battery (TestInjectionBattery style): the new domains
        keep an executable-safe probe targeting the right bundle id."""
        self.assertTrue({"calendar", "music"} <= set(PROBE_SCRIPTS))
        for domain in ("calendar", "music"):
            source = PROBE_SCRIPTS[domain]
            self.assertIn("on run argv", source, domain)  # multi-line const: containment, not position
            self.assertNotIn("do shell script", source, domain)
            self.assertNotIn("run script", source, domain)
        self.assertIn("com.apple.iCal", PROBE_SCRIPTS["calendar"])
        self.assertIn("com.apple.Music", PROBE_SCRIPTS["music"])

    def test_mock_calendar_add_list_delete_observability(self):
        """Mock world keeps add -> list -> delete observable; events use
        future dates computed from now() so the range filter is deterministic.
        An unknown-calendar add must never yield an observable event."""
        try:
            os.environ["NOTIFYBOT_CONNECTORS_MOCK"] = "1"
        finally:
            pass
        try:
            manager = ConnectorManager(self.tmp.name)  # env mock runner active
            manager.state.record("calendar", "granted")
            self.assertEqual(
                manager.run_action("calendar", "list_calendars", {}),
                {"calendars": ["Pessoal", "Trabalho"]},
            )
            start = (datetime.now() + timedelta(days=3)).replace(
                hour=10, minute=0, second=0, microsecond=0
            )
            end = start + timedelta(hours=1)
            start_text = start.strftime("%Y-%m-%d %H:%M")
            end_text = end.strftime("%Y-%m-%d %H:%M")
            added = manager.run_action(
                "calendar", "add_event",
                {"summary": "Reunião de equipe", "start": start_text, "end": end_text,
                 "calendar": "Trabalho", "notes": "Trazer pão", "alert_minutes": 10},
            )
            self.assertEqual(
                added,
                {"ok": True, "summary": "Reunião de equipe", "start": start_text, "calendar": "Trabalho"},
            )
            listing = manager.run_action("calendar", "list_events", {"calendar": "Trabalho", "days": 30})
            self.assertEqual(
                listing,
                {"calendar": "Trabalho", "events": [
                    {"summary": "Reunião de equipe",
                     "start": start_text.replace(" ", "T"),
                     "end": end_text.replace(" ", "T")},
                ]},
            )
            # unknown calendar: the mock exit-1 failure now classifies through
            # the SAME _ScriptFailure/_action_failure_error path as the real
            # runner (no silent ghost-success), and delete fails the same way
            # — never via a misleading "delete count" parse of empty stdout.
            with self.assertRaises(ConnectorError) as ghost_ctx:
                manager.run_action(
                    "calendar", "add_event",
                    {"summary": "Evento Fantasma", "start": start_text, "end": end_text,
                     "calendar": "Nope"},
                )
            self.assertIn("Can't get calendar", str(ghost_ctx.exception))
            with self.assertRaises(ConnectorError) as ghost_del_ctx:
                manager.run_action(
                    "calendar", "delete_event",
                    {"summary": "Evento Fantasma", "calendar": "Nope"},
                )
            self.assertIn("Calendar action failed", str(ghost_del_ctx.exception))
            self.assertNotIn("delete count was not an integer", str(ghost_del_ctx.exception))
            # the ghost event must not appear in ANY view; the real one must
            self.assertEqual(
                manager.run_action("calendar", "list_events", {}),
                {"events": [{"summary": "Reunião de equipe",
                             "start": start_text.replace(" ", "T"),
                             "end": end_text.replace(" ", "T")}]},
            )
            self.assertEqual(
                manager.run_action("calendar", "list_events", {"calendar": "Nope", "days": 30}),
                {"calendar": "Nope", "events": []},
            )
            self.assertEqual(
                manager.run_action("calendar", "list_events", {"calendar": "Trabalho", "days": 30}),
                {"calendar": "Trabalho", "events": [
                    {"summary": "Reunião de equipe",
                     "start": start_text.replace(" ", "T"),
                     "end": end_text.replace(" ", "T")},
                ]},
            )
            # EXACT match semantics: a substring summary deletes NOTHING.
            self.assertEqual(
                manager.run_action(
                    "calendar", "delete_event",
                    {"summary": "Reunião", "calendar": "Trabalho"},
                ),
                {"ok": True, "deleted": 0},
            )
            self.assertEqual(
                manager.run_action(
                    "calendar", "delete_event",
                    {"summary": "Reunião de equipe", "calendar": "Trabalho"},
                ),
                {"ok": True, "deleted": 1},
            )
            self.assertEqual(
                manager.run_action(
                    "calendar", "delete_event",
                    {"summary": "Reunião de equipe", "calendar": "Trabalho"},
                ),
                {"ok": True, "deleted": 0},  # second identical delete: nothing left
            )
            self.assertEqual(
                manager.run_action("calendar", "list_events", {"calendar": "Trabalho", "days": 30}),
                {"calendar": "Trabalho", "events": []},
            )
        finally:
            os.environ.pop("NOTIFYBOT_CONNECTORS_MOCK", None)

    def test_mock_music_playback_volume_search_observability(self):
        """Mock world: playback state transitions, playlist not-found copy,
        volume get/set round-trip, and search rows stay observable."""
        try:
            os.environ["NOTIFYBOT_CONNECTORS_MOCK"] = "1"
        finally:
            pass
        try:
            manager = ConnectorManager(self.tmp.name)  # env mock runner active
            manager.state.record("music", "granted")
            self.assertEqual(
                manager.run_action("music", "status", {}),
                {"state": "paused", "name": "", "artist": "", "album": "", "position": 0, "duration": 0},
            )
            self.assertEqual(manager.run_action("music", "play", {}), {"ok": True})
            self.assertEqual(manager.run_action("music", "status", {})["state"], "playing")
            self.assertEqual(manager.run_action("music", "pause", {}), {"ok": True})
            self.assertEqual(manager.run_action("music", "status", {})["state"], "paused")
            self.assertEqual(
                manager.run_action("music", "play_playlist", {"name": "Foco"}),
                {"ok": True, "playlist": "Foco"},
            )
            playing = manager.run_action("music", "status", {})
            self.assertEqual(playing["state"], "playing")
            self.assertEqual(playing["name"], "Foco")
            with self.assertRaises(ConnectorError) as ctx:
                manager.run_action("music", "play_playlist", {"name": "Nope"})
            self.assertEqual(
                str(ctx.exception), "Playlist 'Nope' não encontrada (verifique o nome exato)."
            )
            self.assertEqual(manager.run_action("music", "volume", {}), {"level": 50})
            self.assertEqual(
                manager.run_action("music", "volume", {"level": 70}), {"ok": True, "level": 70}
            )
            self.assertEqual(manager.run_action("music", "volume", {}), {"level": 70})
            self.assertEqual(
                manager.run_action("music", "search_store", {"query": "qualquer"}),
                {"results": [
                    {"name": "Track A", "artist": "Artist A", "album": "Album A"},
                    {"name": "Track B", "artist": "Artist B", "album": "Album B"},
                ]},
            )
        finally:
            os.environ.pop("NOTIFYBOT_CONNECTORS_MOCK", None)

    def test_reset_mock_world_reseeds_calendar_and_music(self):
        """After reset the world returns to the per-manager seeded state:
        calendars back to seed order, no stray events, paused music at 50."""
        try:
            os.environ["NOTIFYBOT_CONNECTORS_MOCK"] = "1"
        finally:
            pass
        try:
            manager = ConnectorManager(self.tmp.name)  # env mock runner active
            manager.state.record("calendar", "granted")
            manager.state.record("music", "granted")
            event_start = (datetime.now() + timedelta(days=3)).replace(
                hour=10, minute=0, second=0, microsecond=0
            )
            event_end = event_start + timedelta(hours=1)
            manager.run_action(
                "calendar", "add_event",
                {"summary": "Evento X", "start": event_start.strftime("%Y-%m-%d %H:%M"),
                 "end": event_end.strftime("%Y-%m-%d %H:%M"), "calendar": "Pessoal"},
            )
            manager.run_action("music", "play_playlist", {"name": "Foco"})
            manager.run_action("music", "volume", {"level": 70})
            manager.reset_mock_world()
            self.assertEqual(
                manager.run_action("calendar", "list_calendars", {}),
                {"calendars": ["Pessoal", "Trabalho"]},
            )
            self.assertEqual(
                manager.run_action("calendar", "list_events", {"calendar": "Pessoal", "days": 30}),
                {"calendar": "Pessoal", "events": []},
            )
            self.assertEqual(
                manager.run_action("music", "status", {}),
                {"state": "paused", "name": "", "artist": "", "album": "", "position": 0, "duration": 0},
            )
            self.assertEqual(manager.run_action("music", "volume", {}), {"level": 50})
        finally:
            os.environ.pop("NOTIFYBOT_CONNECTORS_MOCK", None)

    def test_mock_failure_outcomes_classify_denied_and_error(self):
        """Seam regression: mock outcomes with rc != 0 flow through the SAME
        _ScriptFailure classification as the real runner. A -1743 stderr is a
        TCC denial -> persists denied; a plain error never downgrades state."""
        try:
            os.environ["NOTIFYBOT_CONNECTORS_MOCK"] = "1"
        finally:
            pass
        try:
            manager = ConnectorManager(self.tmp.name)  # env mock runner active
            manager.state.record("calendar", "granted")
            # denial path: -1743 stderr classifies "denied" AND persists it
            manager._mock_runner = lambda cmd, timeout, stdin=None: Outcome(
                1, "", "Not authorized to send Apple events (-1743)"
            )
            with self.assertRaises(ConnectorError) as denied_ctx:
                manager.run_action("calendar", "list_calendars", {})
            self.assertIn("-1743", str(denied_ctx.exception))
            self.assertIn("Settings › Connectors", str(denied_ctx.exception))
            self.assertEqual(manager.state.read()["calendar"]["status"], "denied")
            # generic error path: rc != 0 without denial markers stays "error" —
            # the copy says "action failed" and the granted state is NOT flipped
            manager.state.record("calendar", "granted")
            manager._mock_runner = lambda cmd, timeout, stdin=None: Outcome(3, "", "boom")
            with self.assertRaises(ConnectorError) as error_ctx:
                manager.run_action("calendar", "list_calendars", {})
            self.assertIn("action failed", str(error_ctx.exception))
            self.assertIn("(exit 3)", str(error_ctx.exception))
            self.assertEqual(manager.state.read()["calendar"]["status"], "granted")
        finally:
            os.environ.pop("NOTIFYBOT_CONNECTORS_MOCK", None)

    def test_music_probe_classification_denied_timeout_and_granted(self):
        """Music probe classification via the runner seam: a catchable -1743
        reaching the probe script must classify "denied" (NOT granted); the
        healthy no-track path is "granted"; a hang is "needs_consent"."""
        runner = RecordingRunner()
        runner.register(
            PROBE_SCRIPTS["music"], Outcome(1, "", "Not authorized to send Apple events (-1743)")
        )
        runner.register(PROBE_SCRIPTS["calendar"], Outcome(0, "5", ""))
        manager = ConnectorManager(self.tmp.name, runner=runner)
        self.assertEqual(manager.probe("music")["status"], "denied")
        self.assertEqual(manager.state.read()["music"]["status"], "denied")
        # healthy no-track path: rc 0 -> granted (state re-recorded unknown first)
        manager.state.record("music", "unknown")
        runner.register(PROBE_SCRIPTS["music"], Outcome(0, "false", ""))
        self.assertEqual(manager.probe("music")["status"], "granted")
        self.assertEqual(manager.state.read()["music"]["status"], "granted")
        # calendar rides the same machinery, unguarded: rc 0 -> granted
        self.assertEqual(manager.probe("calendar")["status"], "granted")
        # timeout -> needs_consent (persisted), like the reminders probe split
        manager.state.record("music", "unknown")

        def timeout_runner(cmd, timeout, stdin=None):
            raise subprocess.TimeoutExpired(cmd, timeout)

        manager._runner = timeout_runner
        self.assertEqual(manager.probe("music")["status"], "needs_consent")
        self.assertEqual(manager.state.read()["music"]["status"], "needs_consent")


# ---- shortcuts + mail domains: dispatch, gating, probe, mock world --------------------
# Imports for the shortcuts/mail suites below: appended here (not merged into
# the top block) so this change stays a pure addition to the file (FASE 1
# precedent above).

from notifybot.connectors import (
    CONNECTOR_ACTIONS,
    GATED_DOMAINS,
    HINT_SHORTCUTS,
    LIST_NAME_MAX_CHARS,  # noqa: F811  (re-import with the FASE-2 family for locality)
    MAIL_ERROR_MARKER,
    MAIL_LIST_MAX_COUNT,
    MAIL_LIST_RECENT,
    MAIL_MAILBOXES,
    MAIL_MISSING_MAILBOX_MARKER,
    MAIL_UNREAD_COUNT,
    PROBE_COMMANDS,
    SHORTCUTS_INPUT_MAX_CHARS,
    SHORTCUTS_OUTPUT_MAX_BYTES,
    SHORTCUTS_TIMEOUT_S,
    _ScriptFailure,
    _default_runner,
)


class TestShortcutsDispatchActions(unittest.TestCase):
    """CLI-domain dispatch: 10 s timeout reach-through, stdin seam, 4096 B
    output cap, unknown-name pre-check ordering, and validation copy. NOT
    gated — every action runs with an untouched consent state."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.names = ["Resumo da semana", "Abrir agenda", "Backup das fotos"]
        self.runner = RecordingRunner()
        # RecordingRunner keys outcomes on cmd[2]-when-present: the 2-arg
        # `shortcuts list` command has no cmd[2], so it matches the "" key;
        # a run command's cmd[2] is the shortcut NAME.
        self.runner.register("", Outcome(0, "\n".join(self.names) + "\n", ""))
        self.runner.register("Resumo da semana", Outcome(0, "atalho executado", ""))
        self.manager = ConnectorManager(self.tmp.name, runner=self.runner)

    def test_list_argv_shape_names_parse_and_timeout_reach_through(self):
        result = self.manager.run_action("shortcuts", "list", {})
        self.assertEqual(result, {"names": self.names})
        self.assertEqual(self.runner.calls[-1], ["shortcuts", "list"])
        self.assertEqual(len(self.runner.calls[0]), 2)  # argv ends after "list"
        self.assertEqual(self.runner.timeout_seen, SHORTCUTS_TIMEOUT_S)

    def test_run_argv_precheck_order_and_stdin_seam(self):
        result = self.manager.run_action("shortcuts", "run", {"name": "Resumo da semana"})
        self.assertEqual(result, {"output": "atalho executado", "exit_code": 0})
        # pre-check listing ran first, then the run; timeout is CLI-tuned
        self.assertEqual(self.runner.calls[-2], ["shortcuts", "list"])
        self.assertEqual(self.runner.calls[-1], ["shortcuts", "run", "Resumo da semana"])
        self.assertIsNone(self.runner.last_stdin())
        # input_text: argv gains -x AND the text is paged via the stdin seam
        result2 = self.manager.run_action(
            "shortcuts", "run", {"name": "Resumo da semana", "input_text": "carga"}
        )
        self.assertEqual(result2, {"output": "atalho executado", "exit_code": 0})
        self.assertEqual(self.runner.calls[-1], ["shortcuts", "run", "Resumo da semana", "-x"])
        self.assertEqual(self.runner.last_stdin(), "carga")

    def test_run_validation_english_and_runner_never_invoked(self):
        cases = [
            ({}, "'name' is required"),
            ({"name": None}, "'name' is required"),
            ({"name": "a" * (LIST_NAME_MAX_CHARS + 1)}, "'name' is limited to 256 characters"),
            ({"name": "Resumo da semana", "input_text": "c" * (SHORTCUTS_INPUT_MAX_CHARS + 1)},
             "'input_text' is limited to 512 characters"),
            ({"name": "Resumo da semana", "stdin": "pipe"}, 'stdin only accepts the value "text".'),
            ({"name": "Resumo da semana", "stdin": "text"},
             'stdin="text" requires an input_text param.'),
        ]
        for params, expected in cases:
            with self.assertRaises(ConnectorError) as ctx:
                self.manager.run_action("shortcuts", "run", params)
            self.assertEqual(str(ctx.exception), expected, params)
            self.assertEqual(self.runner.calls, [])  # validation never dispatched anything

    def test_unknown_name_clean_error_and_no_run_dispatch(self):
        with self.assertRaises(ConnectorError) as ctx:
            self.manager.run_action("shortcuts", "run", {"name": "Fantasma"})
        self.assertEqual(
            str(ctx.exception),
            'Atalho não encontrado — use a ação "list" para ver os atalhos '
            "disponíveis.",
        )
        # pre-check ordering: ONLY the `shortcuts list` call exists — the run
        # command was never dispatched (runner.calls never holds it).
        self.assertEqual(self.runner.calls, [["shortcuts", "list"]])

    def test_run_output_byte_cap_on_utf8_boundary(self):
        self.runner.register(
            "Resumo da semana", Outcome(0, "x" * (SHORTCUTS_OUTPUT_MAX_BYTES + 1), "")
        )
        result = self.manager.run_action("shortcuts", "run", {"name": "Resumo da semana"})
        self.assertEqual(len(result["output"].encode("utf-8")), SHORTCUTS_OUTPUT_MAX_BYTES)
        # multibyte safety: a broken code point must never be emitted
        self.runner.register("Resumo da semana", Outcome(0, "ç" * 2500, ""))
        result2 = self.manager.run_action("shortcuts", "run", {"name": "Resumo da semana"})
        self.assertEqual(len(result2["output"].encode("utf-8")), SHORTCUTS_OUTPUT_MAX_BYTES)
        self.assertEqual(result2["output"], "ç" * 2048)  # 2048 * 2 B = 4096 B

    def test_ungated_runs_without_touching_consent_state(self):
        self.assertEqual(self.manager.state.read()["shortcuts"]["status"], "unknown")
        self.assertEqual(self.manager.run_action("shortcuts", "list", {}), {"names": self.names})
        # success did NOT self-heal/state-record anything for the CLI domain
        self.assertEqual(self.manager.state.read()["shortcuts"]["status"], "unknown")

    def test_run_timeout_never_probed_state_renders_granted_copy_without_consent(self):
        """FIX-2 regression: a `run` timeout on a never-probed (unknown) state
        renders the CLI cold-start wording (granted side of the split) with NO
        Settings/Connect remediation — the consent flow does not exist for the
        ungated shortcuts domain."""
        run_timeouts = []

        def run_timeout_runner(cmd, timeout, stdin=None):
            if cmd[0] == "shortcuts" and cmd[1] == "run":
                run_timeouts.append(timeout)
                raise subprocess.TimeoutExpired(cmd, timeout)
            return Outcome(0, "\n".join(self.names) + "\n", "")

        manager = ConnectorManager(self.tmp.name + "-timeout", runner=run_timeout_runner)
        self.assertEqual(manager.state.read()["shortcuts"]["status"], "unknown")
        with self.assertRaises(ConnectorError) as ctx:
            manager.run_action("shortcuts", "run", {"name": "Resumo da semana"})
        message = str(ctx.exception)
        # granted-branch copy: retry wording only
        self.assertEqual(
            message,
            "Shortcuts não respondeu a tempo — possível primeiro uso/sincronização; "
            "tente de novo que eu reexecuto.",
        )
        self.assertNotIn("Settings › Connectors", message)
        self.assertNotIn("Connect", message)
        # the run call was threaded the CLI timeout, not the shared 8 s
        self.assertEqual(run_timeouts, [SHORTCUTS_TIMEOUT_S])
        # a timeout hitting the pre-check first renders the same copy
        def timeout_runner(cmd, timeout, stdin=None):
            raise subprocess.TimeoutExpired(cmd, timeout)

        manager2 = ConnectorManager(self.tmp.name + "-timeout2", runner=timeout_runner)
        with self.assertRaises(ConnectorError) as ctx2:
            manager2.run_action("shortcuts", "run", {"name": "Resumo da semana"})
        self.assertEqual(str(ctx2.exception), str(ctx.exception))
        self.assertNotIn("Settings › Connectors", str(ctx2.exception))
        # and the 10 s timeout is threaded into the seconds rendering when the
        # timed branch fires (the granted copy carries no seconds — it stays
        # byte-identical for every domain):
        self.assertIn(
            "não respondeu em 10s",
            str(
                ConnectorManager._action_failure_error(
                    "shortcuts",
                    _ScriptFailure("timeout", 0, ""),
                    granted=False,
                    timeout=SHORTCUTS_TIMEOUT_S,
                )
            ),
        )
        # the default keeps the five pre-existing domains byte-identical (8 s)
        self.assertIn(
            "não respondeu em 8s",
            str(
                ConnectorManager._action_failure_error(
                    "music", _ScriptFailure("timeout", 0, "")
                )
            ),
        )

    def test_default_runner_real_subprocess_stdin_seam_and_neutral_oserror(self):
        """FIX-3/FIX-4 regressions: the prod runner seam really pipes stdin via
        subprocess input= (not just the RecordingRunner fixture), and the
        OSError prefix is domain-neutral with the exception detail preserved."""
        outcome = _default_runner(["cat"], 5.0, "ping")
        self.assertEqual(outcome.returncode, 0)
        self.assertEqual(outcome.stdout.strip(), "ping")
        # a missing binary surfaces through the same domain-neutral wording
        manager = ConnectorManager(self.tmp.name + "-oserror")  # real runner (no env mock)
        with self.assertRaises(ConnectorError) as ctx:
            manager._execute(["definitely-missing-binary-fase2-check"])
        self.assertIn("command execution failed", str(ctx.exception))
        self.assertIn("definitely-missing-binary-fase2-check", str(ctx.exception))
        self.assertNotIn("osascript execution failed", str(ctx.exception))


class TestMailDispatchActions(unittest.TestCase):
    """Read-level-1 mail dispatch: argv layout, chunk rows, marker parsing,
    validation caps, and the GATED consent + probe machinery."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.runner = RecordingRunner()
        self.manager = ConnectorManager(self.tmp.name, runner=self.runner)
        self.manager.state.record("mail", "granted")

    def test_unread_count_argv_order_and_result(self):
        self.runner.register(MAIL_UNREAD_COUNT, Outcome(0, "3", ""))
        self.assertEqual(self.manager.run_action("mail", "unread_count", {}), {"unread": 3})
        self.assertEqual(self.runner.calls[-1][2], MAIL_UNREAD_COUNT)
        self.assertEqual(self.runner.last_argv(), ["", ""])  # (mailbox, account) with "" defaults
        with_params = self.manager.run_action(
            "mail", "unread_count", {"mailbox": "Work INBOX", "account": "fulano@exemplo.com"}
        )
        self.assertEqual(with_params, {"unread": 3})
        self.assertEqual(self.runner.last_argv(), ["Work INBOX", "fulano@exemplo.com"])

    def test_mailboxes_single_script_argv_and_split(self):
        self.runner.register(
            MAIL_MAILBOXES, Outcome(0, _DELIM.join(["INBOX", "Enviados", "Arquivo"]), "")
        )
        self.assertEqual(
            self.manager.run_action("mail", "mailboxes", {}),
            {"mailboxes": ["INBOX", "Enviados", "Arquivo"]},
        )
        cmd = self.runner.calls[-1]
        self.assertEqual(cmd[:3], ["osascript", "-e", MAIL_MAILBOXES])
        self.assertEqual(cmd[3:], [])  # argv ends after the script constant

    def test_list_recent_argv_chunk_rows_and_default_count(self):
        self.runner.register(
            MAIL_LIST_RECENT,
            Outcome(0, _DELIM.join(["S1", "F1", "D1", "S2", "F2", "D2"]), ""),
        )
        result = self.manager.run_action("mail", "list_recent", {"mailbox": "Work INBOX"})
        self.assertEqual(
            result,
            {"messages": [
                {"subject": "S1", "from": "F1", "received": "D1"},
                {"subject": "S2", "from": "F2", "received": "D2"},
            ]},
        )
        self.assertEqual(self.runner.last_argv(), ["Work INBOX", "20"])  # default count 20
        self.manager.run_action("mail", "list_recent", {"mailbox": "Work INBOX", "count": 5})
        self.assertEqual(self.runner.last_argv(), ["Work INBOX", "5"])
        upper = self.manager.run_action("mail", "list_recent", {"count": MAIL_LIST_MAX_COUNT})
        self.assertEqual(self.runner.last_argv(), ["", "20"])  # boundary 20 is VALID
        self.assertEqual(len(upper["messages"]), 2)

    def test_list_recent_count_validation_error_not_clamp(self):
        with self.assertRaises(ConnectorError) as ctx:
            self.manager.run_action("mail", "list_recent", {"count": MAIL_LIST_MAX_COUNT + 1})
        self.assertEqual(str(ctx.exception), "count accepts at most 20 messages.")
        for bad in (0, -1, 2.5, True, "abc"):
            with self.assertRaises(ConnectorError) as pos_ctx:
                self.manager.run_action("mail", "list_recent", {"count": bad})
            self.assertEqual(
                str(pos_ctx.exception), "'count' must be a positive integer", bad
            )
        with self.assertRaises(ConnectorError) as len_ctx:
            self.manager.run_action(
                "mail", "list_recent", {"mailbox": "m" * (LIST_NAME_MAX_CHARS + 1)}
            )
        self.assertIn("limited to 256", str(len_ctx.exception))
        self.assertEqual(self.runner.calls, [])  # every rejection predates dispatch

    def test_missing_mailbox_marker_maps_to_clean_ptbr_copy(self):
        self.runner.register(MAIL_UNREAD_COUNT, Outcome(0, MAIL_MISSING_MAILBOX_MARKER, ""))
        with self.assertRaises(ConnectorError) as ctx:
            self.manager.run_action("mail", "unread_count", {"mailbox": "Não Existe"})
        self.assertEqual(
            str(ctx.exception),
            'Mailbox não encontrada — use a ação "mailboxes" para ver os nomes '
            "disponíveis.",
        )
        self.runner.register(MAIL_LIST_RECENT, Outcome(0, MAIL_MISSING_MAILBOX_MARKER, ""))
        with self.assertRaises(ConnectorError) as ctx2:
            self.manager.run_action("mail", "list_recent", {"mailbox": "Não Existe"})
        self.assertEqual(
            str(ctx2.exception),
            'Mailbox não encontrada — use a ação "mailboxes" para ver os nomes '
            "disponíveis.",
        )
        # argv was still recorded for the failing executions (mirror music)
        self.assertEqual(self.runner.last_argv(), ["Não Existe", "20"])

    def test_error_and_no_account_markers_map_to_clean_copies(self):
        self.runner.register(MAIL_UNREAD_COUNT, Outcome(0, MAIL_ERROR_MARKER, ""))
        with self.assertRaises(ConnectorError) as ctx:
            self.manager.run_action("mail", "unread_count", {})
        self.assertEqual(str(ctx.exception), "Mail action failed: unexpected AppleScript error.")
        self.runner.register(MAIL_UNREAD_COUNT, Outcome(0, NO_ACCOUNT_MARKER, ""))
        with self.assertRaises(ConnectorError) as ctx2:
            self.manager.run_action("mail", "unread_count", {"account": "fantasma"})
        self.assertEqual(
            str(ctx2.exception),
            "Nenhuma conta configurada — configure uma conta no Mail primeiro.",
        )

    def test_ungranted_gate_never_reaches_runner_exact_copy(self):
        gate_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(gate_tmp.cleanup)
        gate_runner = RecordingRunner()
        gate_runner.register(MAIL_UNREAD_COUNT, Outcome(0, "0", ""))
        manager = ConnectorManager(gate_tmp.name, runner=gate_runner)
        for status in ("unknown", "denied", "needs_consent"):
            manager.state.record("mail", status)
            self.assertEqual(gate_runner.calls, [])
            with self.assertRaises(ConnectorError) as ctx:
                manager.run_action("mail", "unread_count", {})
            # gate copy flows from the existing PT-BR machinery verbatim
            self.assertEqual(
                str(ctx.exception),
                "Mail ainda não está conectado — conecte mail em Settings › Connectors.",
            )
            self.assertEqual(gate_runner.calls, [])  # runner never invoked on the gate

    def test_probe_mail_grant_and_denial_classification(self):
        granted_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(granted_tmp.cleanup)
        granted_runner = RecordingRunner()
        granted_runner.register(PROBE_SCRIPTS["mail"], Outcome(0, "0,0", ""))
        manager = ConnectorManager(granted_tmp.name, runner=granted_runner)
        self.assertEqual(manager.probe("mail")["status"], "granted")
        self.assertEqual(manager.state.read()["mail"]["status"], "granted")
        # canned -1743 denial: classified AND persisted (mirror calendar/music)
        denied_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(denied_tmp.cleanup)
        denied_runner = RecordingRunner()
        denied_runner.register(
            PROBE_SCRIPTS["mail"], Outcome(1, "", "Not authorized to send Apple events (-1743)")
        )
        manager2 = ConnectorManager(denied_tmp.name, runner=denied_runner)
        self.assertEqual(manager2.probe("mail")["status"], "denied")
        self.assertEqual(manager2.state.read()["mail"]["status"], "denied")


class TestShortcutsMailGatingProbeAndMockWorld(unittest.TestCase):
    """Membership batteries, probe-registration sanity, and the env-gated
    mock world observability for the shortcuts + mail domains."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_domain_membership_and_gate_lists(self):
        self.assertIn("shortcuts", CONNECTOR_DOMAINS)
        self.assertIn("mail", CONNECTOR_DOMAINS)
        self.assertEqual(CONNECTOR_DOMAINS.index("shortcuts"), 4)
        self.assertEqual(CONNECTOR_DOMAINS.index("mail"), 5)
        self.assertEqual(CONNECTOR_DOMAINS[10], "system")
        self.assertIn("mail", GATED_DOMAINS)
        self.assertNotIn("shortcuts", GATED_DOMAINS)
        self.assertEqual(CONNECTOR_ACTIONS["shortcuts"], ("list", "run"))
        self.assertEqual(
            CONNECTOR_ACTIONS["mail"], ("unread_count", "mailboxes", "list_recent")
        )

    def test_mail_scripts_error_only_when_named_mailbox_matched_nothing(self):
        """FIX-1 regression: the mail scripts' marker fires ONLY when the
        named mailbox matched ZERO mailboxes across the scanned accounts
        (`mailboxArg is not "" and not mailboxFound`); a mailbox present in
        some but not all accounts counts normally (the old missingMailbox
        conflation is gone)."""
        for script in (MAIL_UNREAD_COUNT, MAIL_LIST_RECENT):
            self.assertIn("set mailboxFound to false", script)
            self.assertIn(
                "(count of sourceMailboxes) is greater than 0 then set mailboxFound to true",
                script,
            )
            self.assertIn(
                'if mailboxArg is not "" and not mailboxFound then return '
                '"connectors-mail-missing-mailbox"',
                script,
            )
            self.assertNotIn("missingMailbox", script)

    def test_probe_registration_and_bundle_battery(self):
        self.assertEqual(PROBE_COMMANDS["shortcuts"], ["shortcuts", "list"])
        self.assertIn("mail", PROBE_SCRIPTS)
        self.assertIn("com.apple.mail", PROBE_SCRIPTS["mail"])
        for script in (MAIL_UNREAD_COUNT, MAIL_MAILBOXES, MAIL_LIST_RECENT):
            self.assertIn("com.apple.mail", script)
            self.assertNotIn("do shell script", script)
            self.assertNotIn("run script", script)
        source = PROBE_SCRIPTS["mail"]
        self.assertNotIn("do shell script", source)
        self.assertNotIn("run script", source)

    def test_rows_shortcuts_ungated_granted_and_mail_gated_unknown(self):
        manager = ConnectorManager(
            self.tmp.name,
            runner=lambda cmd, timeout, stdin=None: Outcome(0, "0", ""),
        )
        rows = manager.rows()["connectors"]
        self.assertEqual(
            set(rows),
            {"reminders", "notes", "calendar", "music", "shortcuts", "mail",
             "contacts", "messages", "finder", "quicktime", "system"},
        )
        self.assertEqual(
            rows["shortcuts"],
            {"status": "granted", "granted": True, "last_probe": None, "hint": HINT_SHORTCUTS},
        )
        self.assertFalse(rows["mail"]["granted"])  # gate machinery still applies

    def test_mock_shortcuts_and_mail_observability(self):
        try:
            os.environ["NOTIFYBOT_CONNECTORS_MOCK"] = "1"
        finally:
            pass
        try:
            manager = ConnectorManager(self.tmp.name)  # env mock runner active
            self.assertEqual(
                manager.run_action("shortcuts", "list", {}),
                {"names": ["Resumo da semana", "Abrir agenda", "Backup das fotos"]},
            )
            self.assertEqual(
                manager.run_action("shortcuts", "run", {"name": "Resumo da semana"}),
                {"output": "atalho executado", "exit_code": 0},
            )
            # unknown shortcut name: clean error, and the run cmd never went
            # to the mock runner (only the pre-check list call did)
            mock_calls = []
            original = manager._mock_runner

            def recording(cmd, timeout, stdin=None):
                mock_calls.append(list(cmd))
                return original(cmd, timeout, stdin)

            manager._mock_runner = recording
            with self.assertRaises(ConnectorError) as ctx:
                manager.run_action("shortcuts", "run", {"name": "Fantasma"})
            self.assertEqual(
                str(ctx.exception),
                'Atalho não encontrado — use a ação "list" para ver os atalhos '
                "disponíveis.",
            )
            self.assertEqual(
                [(cmd[0], cmd[1]) for cmd in mock_calls], [("shortcuts", "list")]
            )
            # mail (gated): consent first, then the read-level-1 fixtures
            manager.state.record("mail", "granted")
            self.assertEqual(manager.run_action("mail", "unread_count", {}), {"unread": 3})
            self.assertEqual(
                manager.run_action("mail", "mailboxes", {}),
                {"mailboxes": ["INBOX", "Enviados", "Arquivo"]},
            )
            self.assertEqual(
                manager.run_action("mail", "list_recent", {}),
                {"messages": [
                    {"subject": "Relatório mensal pronto",
                     "from": "Fulano <fulano@exemplo.com>",
                     "received": "31/10/2026 18:02"},
                    {"subject": "Re: contratos",
                     "from": "Beltrano <beltrano@exemplo.com>",
                     "received": "01/11/2026 09:15"},
                ]},
            )
            with self.assertRaises(ConnectorError) as mbx_ctx:
                manager.run_action("mail", "unread_count", {"mailbox": "Não Existe"})
            self.assertEqual(
                str(mbx_ctx.exception),
                'Mailbox não encontrada — use a ação "mailboxes" para ver os nomes '
                "disponíveis.",
            )
        finally:
            os.environ.pop("NOTIFYBOT_CONNECTORS_MOCK", None)

    def test_reset_mock_world_reseeds_shortcuts_and_mail(self):
        try:
            os.environ["NOTIFYBOT_CONNECTORS_MOCK"] = "1"
        finally:
            pass
        try:
            manager = ConnectorManager(self.tmp.name)  # env mock runner active
            manager.state.record("mail", "granted")
            # mutate BOTH worlds away from the seed...
            manager._mock_mail["unread"] = 99
            manager._mock_mail["mailboxes"].append("Fantasma")
            manager._mock_shortcuts["names"].append("Fantasma")
            manager.reset_mock_world()
            # ...and observe the post-reset seed state end to end
            self.assertEqual(
                manager.run_action("mail", "unread_count", {}),
                {"unread": 3},
            )
            self.assertEqual(
                manager.run_action("mail", "mailboxes", {}),
                {"mailboxes": ["INBOX", "Enviados", "Arquivo"]},
            )
            self.assertEqual(
                manager.run_action("shortcuts", "list", {}),
                {"names": ["Resumo da semana", "Abrir agenda", "Backup das fotos"]},
            )
        finally:
            os.environ.pop("NOTIFYBOT_CONNECTORS_MOCK", None)

    def test_mock_probe_flows_grant_new_domains(self):
        try:
            os.environ["NOTIFYBOT_CONNECTORS_MOCK"] = "1"
        finally:
            pass
        try:
            manager = ConnectorManager(self.tmp.name)  # env mock runner active
            # shortcuts probe goes through PROBE_COMMANDS (CLI list outcome)
            self.assertEqual(manager.probe("shortcuts")["status"], "granted")
            self.assertEqual(manager.state.read()["shortcuts"]["status"], "granted")
            # mail probe rides the trivial-success fallback for its script
            self.assertEqual(manager.probe("mail")["status"], "granted")
            self.assertEqual(manager.state.read()["mail"]["status"], "granted")
            self.assertEqual(manager.rows()["connectors"]["shortcuts"]["hint"], HINT_SHORTCUTS)
        finally:
            os.environ.pop("NOTIFYBOT_CONNECTORS_MOCK", None)

    def test_mock_mail_account_not_found_maps_to_clean_ptbr(self):
        """FIX-5 regression: the env-mock mail world is account-aware — an
        unknown non-empty account yields the NO_ACCOUNT_MARKER outcome, which
        the parser maps to the clean PT-BR account-setup copy; a known account
        proceeds with the mailbox-name rule."""
        try:
            os.environ["NOTIFYBOT_CONNECTORS_MOCK"] = "1"
        finally:
            pass
        try:
            manager = ConnectorManager(self.tmp.name)  # env mock runner active
            manager.state.record("mail", "granted")
            # unknown account (unread_count) -> NO_ACCOUNT_MARKER -> clean copy
            with self.assertRaises(ConnectorError) as ctx:
                manager.run_action("mail", "unread_count", {"account": "Fantasma"})
            self.assertEqual(
                str(ctx.exception),
                "Nenhuma conta configurada — configure uma conta no Mail primeiro.",
            )
            # a known account proceeds with the mailbox rule: missing mailbox
            with self.assertRaises(ConnectorError) as ctx2:
                manager.run_action(
                    "mail", "unread_count", {"account": "iCloud", "mailbox": "Não Existe"}
                )
            self.assertEqual(
                str(ctx2.exception),
                'Mailbox não encontrada — use a ação "mailboxes" para ver os nomes '
                "disponíveis.",
            )
            # known account + no mailbox named -> the seeded unread count
            self.assertEqual(
                manager.run_action("mail", "unread_count", {"account": "Gmail"}),
                {"unread": 3},
            )
            # list_recent carries NO account channel (script signature is
            # plan-verbatim {mailboxArg, limitArg}): its mailbox rule fires and
            # the ignored 'account' param never reaches the runner.
            with self.assertRaises(ConnectorError) as ctx3:
                manager.run_action(
                    "mail", "list_recent", {"account": "Fantasma", "mailbox": "Não Existe"}
                )
            self.assertEqual(
                str(ctx3.exception),
                'Mailbox não encontrada — use a ação "mailboxes" para ver os nomes '
                "disponíveis.",
            )
            self.assertEqual(
                manager.run_action("mail", "list_recent", {}),
                {"messages": [
                    {"subject": "Relatório mensal pronto",
                     "from": "Fulano <fulano@exemplo.com>",
                     "received": "31/10/2026 18:02"},
                    {"subject": "Re: contratos",
                     "from": "Beltrano <beltrano@exemplo.com>",
                     "received": "01/11/2026 09:15"},
                ]},
            )
        finally:
            os.environ.pop("NOTIFYBOT_CONNECTORS_MOCK", None)

    def test_mock_reset_reseeds_mail_accounts(self):
        """reset_mock_world reseeds the FIX-5 accounts key too."""
        try:
            os.environ["NOTIFYBOT_CONNECTORS_MOCK"] = "1"
        finally:
            pass
        try:
            manager = ConnectorManager(self.tmp.name)  # env mock runner active
            manager.state.record("mail", "granted")
            manager._mock_mail["accounts"].append("Fantasma")
            manager.reset_mock_world()
            with self.assertRaises(ConnectorError) as ctx:
                manager.run_action("mail", "unread_count", {"account": "Fantasma"})
            self.assertIn("Nenhuma conta configurada", str(ctx.exception))
        finally:
            os.environ.pop("NOTIFYBOT_CONNECTORS_MOCK", None)


# ---- contacts + messages domains: dispatch, gating, probe, mock world --------------------
# Imports for the contacts/messages suites below: appended here (not merged into
# the top block) so this change stays a pure addition to the file (FASE 1/2
# precedent above).

from notifybot.connectors import (
    BODY_MAX_BYTES,  # noqa: F811  (re-import with the FASE-3 family for locality)
    CONTACTS_CREATE,
    CONTACTS_EMAILS_MAX_COUNT,
    CONTACTS_GET,
    CONTACTS_LIST_LIMIT_DEFAULT,
    CONTACTS_NOT_FOUND_MARKER,
    CONTACTS_PHONES_MAX_COUNT,
    CONTACTS_SEARCH,
    CONNECTOR_ACTIONS,  # noqa: F811
    ConnectorError,  # noqa: F811
    ConnectorManager,  # noqa: F811
    GATED_DOMAINS,  # noqa: F811
    LIST_NAME_MAX_CHARS,  # noqa: F811
    MESSAGES_LIST_THREADS,
    MESSAGES_SEND_CONTACT,
    MESSAGES_SEND_HANDLE,
    MESSAGES_TEXT_MAX_CHARS,
    MESSAGES_THREADS_MAX_COUNT,
    MESSAGES_TO_MAX_CHARS,
    MESSAGES_UNKNOWN_RECIPIENT_MARKER,
    PROBE_SCRIPTS,  # noqa: F811
    QUERY_MAX_CHARS,
    SCRIPTS,  # noqa: F811
    LIST_LIMIT_MAX,  # noqa: F811
    ERR_UNKNOWN_RECIPIENT,
    _DELIM,  # noqa: F811
)


class TestContactsDispatchActions(unittest.TestCase):
    """Contacts dispatch: argv layout, split_keep row parsing (empty fields
    KEEP positions), marker remap, validation caps, and list caps."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.runner = RecordingRunner()
        self.manager = ConnectorManager(self.tmp.name, runner=self.runner)
        self.manager.state.record("contacts", "granted")

    def test_search_default_limit_argv_and_row_parsing(self):
        self.runner.register(
            CONTACTS_SEARCH,
            Outcome(0, _DELIM.join([
                "Marina Duarte", "Imaginária Ltda", "marina@imaginaria.test", "+5511922220001",
                "Bruno Tanaka", "", "bruno@tanaka.test", "",
            ]), ""),
        )
        result = self.manager.run_action("contacts", "search", {"query": "Marina"})
        self.assertEqual(
            result,
            {"contacts": [
                {"name": "Marina Duarte", "org": "Imaginária Ltda",
                 "email": "marina@imaginaria.test", "phone": "+5511922220001"},
                {"name": "Bruno Tanaka", "org": "",  # empty field KEEPS its position (split_keep)
                 "email": "bruno@tanaka.test", "phone": ""},
            ]},
        )
        self.assertEqual(self.runner.calls[-1][2], CONTACTS_SEARCH)
        self.assertEqual(self.runner.last_argv(), ["Marina", "10"])  # default limit 10

    def test_search_limit_param_and_validation(self):
        self.runner.register(CONTACTS_SEARCH, Outcome(0, "", ""))
        self.manager.run_action("contacts", "search", {"query": "x", "limit": 3})
        self.assertEqual(self.runner.last_argv(), ["x", "3"])
        self.manager.run_action("contacts", "search", {"query": "x", "limit": 50})
        self.assertEqual(self.runner.last_argv(), ["x", "50"])  # boundary 50 is VALID
        valid_calls = list(self.runner.calls)  # the two valid probes (never re-run)
        with self.assertRaises(ConnectorError) as ctx:
            self.manager.run_action("contacts", "search", {"query": "x", "limit": 51})
        self.assertEqual(str(ctx.exception), "limit accepts at most 50 contacts.")  # above the cap ERRORS
        for bad in (0, -1, 2.5, True, "abc"):
            with self.assertRaises(ConnectorError) as pos_ctx:
                self.manager.run_action("contacts", "search", {"query": "x", "limit": bad})
            self.assertEqual(str(pos_ctx.exception), "'limit' must be a positive integer", bad)
        with self.assertRaises(ConnectorError) as q_ctx:
            self.manager.run_action("contacts", "search", {"query": "q" * (QUERY_MAX_CHARS + 1)})
        self.assertEqual(str(q_ctx.exception), "'query' is limited to 256 characters")
        with self.assertRaises(ConnectorError) as req_ctx:
            self.manager.run_action("contacts", "search", {"limit": 5})
        self.assertEqual(str(req_ctx.exception), "'query' is required")
        # every rejection predates dispatch — the call list never grew
        self.assertEqual(self.runner.calls, valid_calls)

    def test_get_full_record_parse_and_note_cap(self):
        self.runner.register(
            CONTACTS_GET,
            Outcome(0, _DELIM.join([
                "Marina Duarte", "Imaginária Ltda",
                "marina@imaginaria.test, outro@x.test",
                "+5511922220001, +5511922220002",
                "Prefere contato por e-mail.",
            ]), ""),
        )
        result = self.manager.run_action("contacts", "get", {"name": "Marina Duarte"})
        self.assertEqual(
            result,
            {"contact": {
                "name": "Marina Duarte", "org": "Imaginária Ltda",
                "emails": ["marina@imaginaria.test", "outro@x.test"],
                "phones": ["+5511922220001", "+5511922220002"],
                "notes": "Prefere contato por e-mail.",
            }},
        )
        # second case: the note field capped at BODY_MAX_BYTES (16384) exactly
        # ("ç" = 2 UTF-8 bytes; "ç" * 9000 = 18000 bytes).
        big_note = "ç" * 9000
        self.runner.register(
            CONTACTS_GET, Outcome(0, _DELIM.join(["Pessoa", "", "", "", big_note]), "")
        )
        capped = self.manager.run_action("contacts", "get", {"name": "Pessoa"})
        self.assertEqual(len(capped["contact"]["notes"].encode("utf-8")), BODY_MAX_BYTES)
        self.assertEqual(len(capped["contact"]["notes"].encode("utf-8")), 16384)

    def test_get_not_found_marker_clean_ptbr_copy(self):
        self.runner.register(CONTACTS_GET, Outcome(0, CONTACTS_NOT_FOUND_MARKER, ""))
        with self.assertRaises(ConnectorError) as ctx:
            self.manager.run_action("contacts", "get", {"name": "Fantasma"})
        self.assertEqual(
            str(ctx.exception),
            'Contato não encontrado — use a ação "search" para ver os nomes disponíveis.',
        )
        self.assertEqual(self.runner.last_argv(), ["Fantasma"])  # argv still recorded (mirror mail)

    def test_create_argv_layout_result_and_list_caps(self):
        self.runner.register(CONTACTS_CREATE, Outcome(0, "created", ""))
        result = self.manager.run_action(
            "contacts", "create",
            {"name": "Nova Pessoa", "org": "Imaginária Ltda",
             "emails": ["e1@exemplo.test"],
             "phones": ["+5511000000001", "+5511000000002"]},
        )
        self.assertEqual(result, {"ok": True, "created": "Nova Pessoa"})
        self.assertEqual(
            self.runner.last_argv(),
            ["Nova Pessoa", "Imaginária Ltda",
             "e1@exemplo.test", "", "",
             "+5511000000001", "+5511000000002", ""],  # ""-padding for absent items
        )
        # boundary: 3 emails + 3 phones is VALID
        full = self.manager.run_action(
            "contacts", "create",
            {"name": "Cheia",
             "emails": ["a@a.test", "b@b.test", "c@c.test"],
             "phones": ["+5511000000011", "+5511000000012", "+5511000000013"]},
        )
        self.assertEqual(full, {"ok": True, "created": "Cheia"})
        self.assertEqual(
            self.runner.last_argv(),
            ["Cheia", "", "a@a.test", "b@b.test", "c@c.test",
             "+5511000000011", "+5511000000012", "+5511000000013"],
        )
        # caps ERROR above the cap (never clamped/truncated)
        with self.assertRaises(ConnectorError) as e_ctx:
            self.manager.run_action(
                "contacts", "create",
                {"name": "Cheia", "emails": ["a@a.test", "b@b.test", "c@c.test", "d@d.test"]},
            )
        self.assertEqual(str(e_ctx.exception), "'emails' accepts at most 3 values.")
        with self.assertRaises(ConnectorError) as p_ctx:
            self.manager.run_action(
                "contacts", "create",
                {"name": "Cheia", "phones": ["+5511000000011", "+5511000000012",
                                             "+5511000000013", "+5511000000014"]},
            )
        self.assertEqual(str(p_ctx.exception), "'phones' accepts at most 3 values.")
        with self.assertRaises(ConnectorError) as req_ctx:
            self.manager.run_action("contacts", "create", {"emails": ["a@a.test"]})
        self.assertEqual(str(req_ctx.exception), "'name' is required")
        with self.assertRaises(ConnectorError) as len_ctx:
            self.manager.run_action(
                "contacts", "create", {"name": "n" * (LIST_NAME_MAX_CHARS + 1)}
            )
        self.assertEqual(str(len_ctx.exception), "'name' is limited to 256 characters")
        self.assertEqual(len(self.runner.calls), 2)  # only the two valid creates reached the runner

    def test_create_rejects_delimiter_and_join_collisions(self):
        """Create-time guards: ", " in an email collides with CONTACTS_GET's
        ", "-joined list rendering; _DELIM anywhere in a stored field would
        misalign later split_keep/get parsing. Every rejection predates any
        dispatch."""
        with self.assertRaises(ConnectorError) as join_ctx:
            self.manager.run_action(
                "contacts", "create", {"name": "N", "emails": ["a@x, b@y"]}
            )
        self.assertEqual(str(join_ctx.exception), "'emails' must not contain ', '")
        with self.assertRaises(ConnectorError) as name_ctx:
            self.manager.run_action("contacts", "create", {"name": "a\x1fb"})
        self.assertEqual(
            str(name_ctx.exception),
            "'name' must not contain the record delimiter character",
        )
        with self.assertRaises(ConnectorError) as phones_ctx:
            self.manager.run_action(
                "contacts", "create", {"name": "N", "phones": ["p\x1fq"]}
            )
        self.assertEqual(
            str(phones_ctx.exception),
            "'phones' must not contain the record delimiter character",
        )
        with self.assertRaises(ConnectorError) as join_phones_ctx:
            self.manager.run_action("contacts", "create", {"name": "N", "phones": ["p, q"]})
        self.assertEqual(str(join_phones_ctx.exception), "'phones' must not contain ', '")
        self.assertEqual(self.runner.calls, [])  # nothing valid was registered: zero dispatches

    def test_contacts_unknown_action_copy(self):
        with self.assertRaises(ConnectorError) as ctx:
            self.manager.run_action("contacts", "delete", {"name": "x"})
        self.assertEqual(
            str(ctx.exception),
            "unknown action 'delete' for domain 'contacts' (valid: search, get, create)",
        )
        self.assertEqual(self.runner.calls, [])


class TestMessagesDispatchActions(unittest.TestCase):
    """Messages dispatch: handle/contact script split, refusal-marker mapping,
    validation caps, and the date-only thread privacy floor."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.runner = RecordingRunner()
        self.manager = ConnectorManager(self.tmp.name, runner=self.runner)
        self.manager.state.record("messages", "granted")

    def test_send_email_handle_direct_script(self):
        self.runner.register(MESSAGES_SEND_HANDLE, Outcome(0, "sent", ""))
        result = self.manager.run_action(
            "messages", "send_message", {"to": "fulano@exemplo.com", "text": "oi"}
        )
        self.assertEqual(result, {"ok": True, "sent": True, "to": "fulano@exemplo.com"})
        self.assertEqual(self.runner.calls[-1][2], MESSAGES_SEND_HANDLE)  # raw handle -> direct script
        self.assertEqual(self.runner.last_argv(), ["fulano@exemplo.com", "oi"])

    def test_send_e164_direct_script(self):
        self.runner.register(MESSAGES_SEND_HANDLE, Outcome(0, "sent", ""))
        result = self.manager.run_action(
            "messages", "send_message", {"to": "+5511987654321", "text": "oi"}
        )
        self.assertEqual(result, {"ok": True, "sent": True, "to": "+5511987654321"})
        self.assertEqual(self.runner.calls[-1][2], MESSAGES_SEND_HANDLE)
        self.assertEqual(self.runner.last_argv(), ["+5511987654321", "oi"])

    def test_send_contact_name_lookup_script_and_success(self):
        self.runner.register(MESSAGES_SEND_CONTACT, Outcome(0, "sent", ""))
        result = self.manager.run_action(
            "messages", "send_message", {"to": "Marina Duarte", "text": "oi"}
        )
        self.assertEqual(result, {"ok": True, "sent": True, "to": "Marina Duarte"})
        self.assertEqual(self.runner.calls[-1][2], MESSAGES_SEND_CONTACT)  # name -> lookup script
        self.assertEqual(self.runner.last_argv(), ["Marina Duarte", "oi"])

    def test_send_ambiguous_junk_routes_to_contact_lookup(self):
        self.runner.register(MESSAGES_SEND_CONTACT, Outcome(0, "sent", ""))
        result = self.manager.run_action(
            "messages", "send_message", {"to": "5511-9876", "text": "oi"}
        )
        self.assertEqual(result, {"ok": True, "sent": True, "to": "5511-9876"})
        self.assertEqual(self.runner.calls[-1][2], MESSAGES_SEND_CONTACT)  # neither email nor E.164

    def test_send_negative_handle_shapes_route_to_contact_lookup(self):
        """Hardened handle regex: shapes with whitespace/control chars (incl.
        NUL) or a leading +0 fail BOTH raw-handle checks and route to the
        contact-lookup variant — never MESSAGES_SEND_HANDLE (a NUL argv item
        must fail closed at the lookup, not explode at exec time)."""
        self.runner.register(MESSAGES_SEND_CONTACT, Outcome(0, "sent", ""))
        for payload in ("+0", "++1", "a@b c", "a\xa0@b.com", "a\x00@b"):
            result = self.manager.run_action(
                "messages", "send_message", {"to": payload, "text": "oi"}
            )
            self.assertEqual(result, {"ok": True, "sent": True, "to": payload}, payload)
            self.assertEqual(self.runner.calls[-1][2], MESSAGES_SEND_CONTACT, payload)
        self.assertNotIn(MESSAGES_SEND_HANDLE, [cmd[2] for cmd in self.runner.calls])

    def test_send_unknown_recipient_refusal_exact_copy(self):
        self.runner.register(
            MESSAGES_SEND_CONTACT, Outcome(0, MESSAGES_UNKNOWN_RECIPIENT_MARKER, "")
        )
        with self.assertRaises(ConnectorError) as ctx:
            self.manager.run_action(
                "messages", "send_message", {"to": "Fulano Fantasma", "text": "oi"}
            )
        self.assertEqual(
            str(ctx.exception),
            ERR_UNKNOWN_RECIPIENT.format(to="Fulano Fantasma"),
        )
        self.assertEqual(
            str(ctx.exception),
            "unknown recipient: 'Fulano Fantasma' is not a known contact, e-mail address, "
            "or +E.164 phone",
        )
        self.assertEqual(self.runner.last_argv(), ["Fulano Fantasma", "oi"])  # argv still recorded

    def test_send_validation_caps_and_requirement(self):
        self.runner.register(MESSAGES_SEND_HANDLE, Outcome(0, "sent", ""))
        self.runner.register(MESSAGES_SEND_CONTACT, Outcome(0, "sent", ""))
        with self.assertRaises(ConnectorError) as text_ctx:
            self.manager.run_action(
                "messages", "send_message", {"to": "fulano@exemplo.com", "text": "x" * 1025}
            )
        self.assertEqual(str(text_ctx.exception), "'text' is limited to 1024 characters")
        with self.assertRaises(ConnectorError) as to_ctx:
            self.manager.run_action(
                "messages", "send_message", {"to": "t" * 257, "text": "oi"}
            )
        self.assertEqual(str(to_ctx.exception), "'to' is limited to 256 characters")
        with self.assertRaises(ConnectorError) as text_req_ctx:
            self.manager.run_action("messages", "send_message", {"to": "fulano@exemplo.com"})
        self.assertEqual(str(text_req_ctx.exception), "'text' is required")
        with self.assertRaises(ConnectorError) as to_req_ctx:
            self.manager.run_action("messages", "send_message", {"text": "oi"})
        self.assertEqual(str(to_req_ctx.exception), "'to' is required")
        self.assertEqual(self.runner.calls, [])  # every rejection predates dispatch

    def test_threads_default_cap_argv_and_date_only_rows(self):
        self.runner.register(
            MESSAGES_LIST_THREADS,
            Outcome(0, _DELIM.join([
                "marina@imaginaria.test", "2026-11-07",
                "+5511922220001", "2026-11-06",
            ]), ""),
        )
        result = self.manager.run_action("messages", "list_recent_threads", {})
        self.assertEqual(self.runner.last_argv(), ["10"])  # default == cap == 10
        self.assertEqual(
            result,
            {"threads": [
                {"handle": "marina@imaginaria.test", "date": "2026-11-07"},
                {"handle": "+5511922220001", "date": "2026-11-06"},
            ]},
        )
        for row in result["threads"]:  # privacy floor: EVERY row is handle+date ONLY
            self.assertEqual(set(row), {"handle", "date"})
        self.manager.run_action("messages", "list_recent_threads", {"count": 5})
        self.assertEqual(self.runner.last_argv(), ["5"])
        self.manager.run_action("messages", "list_recent_threads", {"count": 10})
        self.assertEqual(self.runner.last_argv(), ["10"])  # boundary 10 is VALID
        with self.assertRaises(ConnectorError) as ctx:
            self.manager.run_action("messages", "list_recent_threads", {"count": 11})
        self.assertEqual(str(ctx.exception), "count accepts at most 10 threads.")  # never clamped
        for bad in (0, -1, 2.5, True, "abc"):
            with self.assertRaises(ConnectorError) as pos_ctx:
                self.manager.run_action("messages", "list_recent_threads", {"count": bad})
            self.assertEqual(str(pos_ctx.exception), "'count' must be a positive integer", bad)
        self.assertEqual(len(self.runner.calls), 3)  # only the 3 valid reads ran


class TestContactsMessagesGatingProbeAndMockWorld(unittest.TestCase):
    """Membership batteries, gate machinery, probe classification, script
    hygiene, and the env-gated mock world observability for contacts +
    messages."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_contacts_messages_membership_and_gate_lists(self):
        self.assertIn("contacts", CONNECTOR_DOMAINS)
        self.assertIn("messages", CONNECTOR_DOMAINS)
        self.assertEqual(CONNECTOR_DOMAINS.index("contacts"), 6)
        self.assertEqual(CONNECTOR_DOMAINS.index("messages"), 7)
        self.assertEqual(CONNECTOR_DOMAINS[10], "system")
        self.assertIn("contacts", GATED_DOMAINS)
        self.assertIn("messages", GATED_DOMAINS)
        self.assertEqual(CONNECTOR_ACTIONS["contacts"], ("search", "get", "create"))
        self.assertEqual(
            CONNECTOR_ACTIONS["messages"], ("send_message", "list_recent_threads")
        )

    def test_ungranted_gates_never_reach_runner_exact_copy(self):
        gate_runner = RecordingRunner()
        manager = ConnectorManager(self.tmp.name, runner=gate_runner)
        for domain, action, params, expected in (
            ("contacts", "search", {"query": "Marina"},
             "Contacts ainda não está conectado — conecte contacts em Settings › Connectors."),
            ("messages", "send_message", {"to": "Marina", "text": "oi"},
             "Messages ainda não está conectado — conecte messages em Settings › Connectors."),
        ):
            for status in ("unknown", "denied", "needs_consent"):
                manager.state.record(domain, status)
                self.assertEqual(gate_runner.calls, [])
                with self.assertRaises(ConnectorError) as ctx:
                    manager.run_action(domain, action, params)
                # gate copy flows from the existing PT-BR machinery verbatim
                self.assertEqual(str(ctx.exception), expected)
                self.assertEqual(gate_runner.calls, [])  # runner NEVER invoked through the gate

    def test_search_validation_predates_gate_and_runner(self):
        """Param validation fires BEFORE the consent gate (proven FASE-1 seam):
        with contacts UNgranted, a bad-limit search still raises the
        validation copy first and never reaches the runner."""
        gate_runner = RecordingRunner()
        manager = ConnectorManager(self.tmp.name, runner=gate_runner)
        self.assertEqual(manager.state.read()["contacts"]["status"], "unknown")
        with self.assertRaises(ConnectorError) as ctx:
            manager.run_action("contacts", "search", {"query": "x", "limit": 51})
        self.assertEqual(str(ctx.exception), "limit accepts at most 50 contacts.")
        self.assertEqual(gate_runner.calls, [])

    def test_script_hygiene_marker_and_bundle_battery(self):
        for script in (CONTACTS_SEARCH, CONTACTS_GET, CONTACTS_CREATE,
                       MESSAGES_SEND_HANDLE, MESSAGES_SEND_CONTACT, MESSAGES_LIST_THREADS):
            self.assertIn("on run argv", script)  # argv-only input channel
            self.assertNotIn("do shell script", script)
            self.assertNotIn("run script", script)
        for script in (CONTACTS_SEARCH, CONTACTS_GET, CONTACTS_CREATE):
            self.assertIn("com.apple.AddressBook", script)
        for script in (MESSAGES_SEND_HANDLE, MESSAGES_SEND_CONTACT, MESSAGES_LIST_THREADS):
            self.assertIn("com.apple.iChat", script)
        self.assertIn(CONTACTS_NOT_FOUND_MARKER, CONTACTS_GET)
        self.assertIn(MESSAGES_UNKNOWN_RECIPIENT_MARKER, MESSAGES_SEND_CONTACT)
        # F2 ambiguity refusal lives in the frozen script: >1 exact-name
        # matches return the marker immediately (never guess a recipient).
        self.assertIn(
            'is greater than 1 then return "connectors-messages-unknown-recipient"',
            MESSAGES_SEND_CONTACT,
        )
        self.assertIn("count of people", PROBE_SCRIPTS["contacts"])
        self.assertIn("com.apple.AddressBook", PROBE_SCRIPTS["contacts"])
        self.assertIn("count of services", PROBE_SCRIPTS["messages"])
        self.assertIn("com.apple.iChat", PROBE_SCRIPTS["messages"])
        # SCRIPTS equality across all six registered keys (incl. the
        # non-whitelisted send_message_contact variant key)
        self.assertIs(SCRIPTS[("contacts", "search")], CONTACTS_SEARCH)
        self.assertIs(SCRIPTS[("contacts", "get")], CONTACTS_GET)
        self.assertIs(SCRIPTS[("contacts", "create")], CONTACTS_CREATE)
        self.assertIs(SCRIPTS[("messages", "send_message")], MESSAGES_SEND_HANDLE)
        self.assertIs(SCRIPTS[("messages", "send_message_contact")], MESSAGES_SEND_CONTACT)
        self.assertIs(SCRIPTS[("messages", "list_recent_threads")], MESSAGES_LIST_THREADS)

    def test_probes_classification_contacts_and_messages(self):
        granted_runner = RecordingRunner()
        granted_runner.register(PROBE_SCRIPTS["contacts"], Outcome(0, "2", ""))
        manager = ConnectorManager(self.tmp.name, runner=granted_runner)
        self.assertEqual(manager.probe("contacts")["status"], "granted")
        self.assertEqual(manager.state.read()["contacts"]["status"], "granted")
        # canned -1743 denial: classified AND persisted (mirror calendar/music/mail)
        denied_runner = RecordingRunner()
        denied_runner.register(
            PROBE_SCRIPTS["messages"],
            Outcome(1, "", "Not authorized to send Apple events (-1743)"),
        )
        manager2 = ConnectorManager(self.tmp.name, runner=denied_runner)
        self.assertEqual(manager2.probe("messages")["status"], "denied")
        self.assertEqual(manager2.state.read()["messages"]["status"], "denied")
        # a hang classifies needs_consent (persisted) — runner-seam split
        manager2.state.record("messages", "unknown")

        def timeout_runner(cmd, timeout, stdin=None):
            raise subprocess.TimeoutExpired(cmd, timeout)

        manager2._runner = timeout_runner
        self.assertEqual(manager2.probe("messages")["status"], "needs_consent")
        self.assertEqual(manager2.state.read()["messages"]["status"], "needs_consent")

    def test_mock_world_observability_contacts_and_messages(self):
        """Env-mock world: seeded search hit; create -> search finds -> get
        full record; send to the created name -> sent; send to an unknown
        name -> the exact refusal copy; a channel-less contact and an
        ambiguous (duplicated) name both refuse with the exact copy;
        threads == the seeded two rows."""
        try:
            os.environ["NOTIFYBOT_CONNECTORS_MOCK"] = "1"
        finally:
            pass
        try:
            manager = ConnectorManager(self.tmp.name)  # env mock runner active
            manager.state.record("contacts", "granted")
            manager.state.record("messages", "granted")
            # seeded search hit (empty org keeps position — split_keep)
            found = manager.run_action("contacts", "search", {"query": "Marina"})
            self.assertEqual(
                found,
                {"contacts": [
                    {"name": "Marina Duarte", "org": "Imaginária Ltda",
                     "email": "marina@imaginaria.test", "phone": "+5511922220001"},
                ]},
            )
            # create -> search finds -> get full record (note "" for created)
            manager.run_action(
                "contacts", "create",
                {"name": "Dora Prista", "emails": ["dora@prista.test"]},
            )
            found2 = manager.run_action("contacts", "search", {"query": "Dora"})
            self.assertEqual([row["name"] for row in found2["contacts"]], ["Dora Prista"])
            got = manager.run_action("contacts", "get", {"name": "Dora Prista"})
            self.assertEqual(
                got,
                {"contact": {
                    "name": "Dora Prista", "org": "",
                    "emails": ["dora@prista.test"], "phones": [], "notes": "",
                }},
            )
            # get of a seeded contact shows its note
            seeded = manager.run_action("contacts", "get", {"name": "Marina Duarte"})
            self.assertEqual(seeded["contact"]["notes"], "Prefere contato por e-mail.")
            # send to the created name -> the lookup variant says sent
            sent = manager.run_action(
                "messages", "send_message", {"to": "Dora Prista", "text": "oi"}
            )
            self.assertEqual(sent, {"ok": True, "sent": True, "to": "Dora Prista"})
            # send to an unknown name -> the exact refusal copy
            with self.assertRaises(ConnectorError) as ctx:
                manager.run_action(
                    "messages", "send_message", {"to": "Fantasma", "text": "oi"}
                )
            self.assertEqual(
                str(ctx.exception), ERR_UNKNOWN_RECIPIENT.format(to="Fantasma")
            )
            # create with NO emails and NO phones (argv rides 8 items with ""
            # padding — the create test above covers the layout): the lookup
            # variant resolves no sendable handle -> the exact refusal copy.
            manager.run_action("contacts", "create", {"name": "Sem Canais"})
            with self.assertRaises(ConnectorError) as no_channel_ctx:
                manager.run_action(
                    "messages", "send_message", {"to": "Sem Canais", "text": "oi"}
                )
            self.assertEqual(
                str(no_channel_ctx.exception),
                ERR_UNKNOWN_RECIPIENT.format(to="Sem Canais"),
            )
            # TWO contacts sharing the exact name: ambiguous -> refuse with
            # the same copy — the mock mirrors the frozen script's >1-match
            # guard (never guess a recipient).
            manager.run_action("contacts", "create", {"name": "Gemeo Silva"})
            manager.run_action("contacts", "create", {"name": "Gemeo Silva"})
            with self.assertRaises(ConnectorError) as ambiguous_ctx:
                manager.run_action(
                    "messages", "send_message", {"to": "Gemeo Silva", "text": "oi"}
                )
            self.assertEqual(
                str(ambiguous_ctx.exception),
                ERR_UNKNOWN_RECIPIENT.format(to="Gemeo Silva"),
            )
            # threads: the seeded rows verbatim, date-only key sets
            threads = manager.run_action("messages", "list_recent_threads", {})
            self.assertEqual(
                threads,
                {"threads": [
                    {"handle": "marina@imaginaria.test", "date": "2026-11-07"},
                    {"handle": "+5511922220001", "date": "2026-11-06"},
                ]},
            )
            for row in threads["threads"]:
                self.assertEqual(set(row), {"handle", "date"})
        finally:
            os.environ.pop("NOTIFYBOT_CONNECTORS_MOCK", None)

    def test_send_injection_battery_contacts_and_messages(self):
        """Model-controlled strings ride strictly as argv items against the
        FROZEN contacts/messages constants — never inside script source.
        Payloads are never e-mail/E.164-shaped, so each `send_message` trip
        routes to the contact-lookup variant."""
        runner = RecordingRunner()
        runner.register(CONTACTS_CREATE, Outcome(0, "created", ""))
        runner.register(MESSAGES_SEND_CONTACT, Outcome(0, "sent", ""))
        manager = ConnectorManager(self.tmp.name, runner=runner)
        manager.state.record("contacts", "granted")
        manager.state.record("messages", "granted")
        for payload in TestInjectionBattery.PAYLOADS:
            manager.run_action("contacts", "create", {"name": payload, "emails": [payload]})
            cmd = runner.calls[-1]
            script_source = cmd[2]
            argv = cmd[3:]
            self.assertIn(payload, argv, payload)  # verbatim argv delivery
            self.assertEqual(script_source, CONTACTS_CREATE)
            self.assertNotIn(payload, script_source, payload)  # absent from source
            manager.run_action("messages", "send_message", {"to": payload, "text": payload})
            cmd = runner.calls[-1]
            script_source = cmd[2]
            self.assertIn(payload, cmd[3:], payload)  # verbatim argv delivery
            self.assertEqual(script_source, MESSAGES_SEND_CONTACT)
            self.assertNotIn(payload, script_source, payload)  # absent from source
            self.assertNotIn(payload, MESSAGES_SEND_HANDLE, payload)  # never in either variant
        for cmd in runner.calls:  # shape holds for EVERY payload
            self.assertEqual(cmd[:2], ["osascript", "-e"])


# ---- finder + quicktime domains: dispatch, gating, probe, mock world --------------------
# Imports for the finder/quicktime suites below: appended here (not merged into
# the top block) so this change stays a pure addition to the file (FASE 1-3
# precedent above).

from notifybot.connectors import (
    CONNECTOR_ACTIONS,  # noqa: F811
    CONNECTOR_DOMAINS,  # noqa: F811
    ConnectorError,  # noqa: F811
    ConnectorManager,  # noqa: F811
    ERR_FINDER_DST_DIR_MISSING,  # noqa: F811
    ERR_FINDER_DST_EXISTS,  # noqa: F811
    ERR_FINDER_NO_FOLDER,  # noqa: F811
    ERR_FINDER_NOT_FOUND,  # noqa: F811
    ERR_NO_RECORDING,  # noqa: F811
    ERR_PATH_OUTSIDE_ROOTS,  # noqa: F811
    ERR_QT_ALREADY_RECORDING,  # noqa: F811
    ERR_QT_DOC_GONE,  # noqa: F811
    ERR_QT_PLAY_FILE,  # noqa: F811
    ERR_TRASH_HOME_REFUSED,  # noqa: F811
    FINDER_DST_DIR_MISSING_MARKER,  # noqa: F811
    FINDER_DST_EXISTS_MARKER,  # noqa: F811
    FINDER_INFO,  # noqa: F811
    FINDER_LIST,  # noqa: F811
    FINDER_LIST_MAX_ITEMS,  # noqa: F811
    FINDER_MOVE,  # noqa: F811
    FINDER_NOT_FOUND_MARKER,  # noqa: F811
    FINDER_PATH_MAX_CHARS,  # noqa: F811
    FINDER_TRASH,  # noqa: F811
    GATED_DOMAINS,  # noqa: F811
    PROBE_SCRIPTS,  # noqa: F811
    QUICKTIME_DOC_GONE_MARKER,  # noqa: F811
    QUICKTIME_FILE_MAX_CHARS,  # noqa: F811
    QUICKTIME_NEW_AUDIO,  # noqa: F811
    QUICKTIME_PLAY,  # noqa: F811
    QUICKTIME_START_SCREEN,  # noqa: F811
    QUICKTIME_STOP,  # noqa: F811
    SCRIPTS,  # noqa: F811
    _DELIM,  # noqa: F811
)

# Added after the pinned FASE-4 import block (kept verbatim): stdlib + module
# source access for the trash static-comment assertion below.
import inspect  # noqa: E402
import notifybot.connectors  # noqa: E402

_res = lambda p: os.path.realpath(os.path.expanduser(p))


class TestFinderDispatchActions(unittest.TestCase):
    """Finder dispatch: argv layout, argv-only reveal, split_keep info row
    (empty fields KEEP positions), marker remap, strict path validation under
    the allowed roots, and the list cap."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.runner = RecordingRunner()
        self.manager = ConnectorManager(self.tmp.name, runner=self.runner)
        self.manager.state.record("finder", "granted")

    def test_list_argv_layout_and_rows(self):
        self.runner.register(
            FINDER_LIST,
            Outcome(0, _DELIM.join(["Relatório.pdf", "Captura de Tela.png"]), ""),
        )
        result = self.manager.run_action("finder", "list", {"path": "~/Desktop"})
        cmd = self.runner.calls[-1]
        self.assertEqual(cmd[:2], ["osascript", "-e"])
        self.assertIs(cmd[2], FINDER_LIST)  # script identity (SCRIPTS-pin precedent)
        self.assertEqual(cmd[3:], [_res("~/Desktop")])
        self.assertEqual(result, {"items": ["Relatório.pdf", "Captura de Tela.png"]})

    def test_list_default_path_is_desktop_and_caps_at_100(self):
        self.runner.register(
            FINDER_LIST,
            Outcome(0, _DELIM.join("item %02d" % i for i in range(101)), ""),
        )
        result = self.manager.run_action("finder", "list", {})
        self.assertEqual(self.runner.last_argv(), [_res("~/Desktop")])  # empty params default
        self.assertEqual(len(result["items"]), FINDER_LIST_MAX_ITEMS)  # hard trim at 100
        self.assertEqual(
            result["items"], ["item %02d" % i for i in range(FINDER_LIST_MAX_ITEMS)]
        )  # the FIRST 100 rows are kept, never reflowed

    def test_reveal_is_argv_only_open_R(self):
        resolved = _res("~/Desktop/Relatório.pdf")
        self.runner.register(resolved, Outcome(0, "", ""))  # reveal's cmd[2] IS the path
        result = self.manager.run_action("finder", "reveal", {"path": "~/Desktop/Relatório.pdf"})
        self.assertEqual(self.runner.calls[-1][:1], ["open"])  # proves the no-osascript route
        self.assertEqual(self.runner.calls[-1], ["open", "-R", resolved])
        self.assertEqual(result, {"ok": True, "path": resolved})

    def test_info_split_keep_row_and_missing_marker(self):
        self.runner.register(
            FINDER_INFO,
            Outcome(
                0,
                _DELIM.join(["PDF document", "20480", "", "31/10/2026 18:02", "01/11/2026 09:15"]),
                "",
            ),
        )
        result = self.manager.run_action("finder", "info", {"path": "~/Desktop/Relatório.pdf"})
        self.assertEqual(
            result,
            {"info": {
                "kind": "PDF document", "size": "20480", "extension": "",
                "created": "31/10/2026 18:02", "modified": "01/11/2026 09:15",
            }},
        )  # empty extension KEEPS its position (split_keep, whitespace reflection)
        # bare marker stdout remaps to the clean refusal copy
        self.runner.register(FINDER_INFO, Outcome(0, FINDER_NOT_FOUND_MARKER, ""))
        with self.assertRaises(ConnectorError) as ctx:
            self.manager.run_action("finder", "info", {"path": "~/Desktop/Fantasma"})
        self.assertEqual(
            str(ctx.exception), ERR_FINDER_NOT_FOUND.format(path=_res("~/Desktop/Fantasma"))
        )

    def test_move_marker_mapping_predates_gate(self):
        self.runner.register(FINDER_MOVE, Outcome(0, FINDER_DST_DIR_MISSING_MARKER, ""))
        with self.assertRaises(ConnectorError) as dir_ctx:
            self.manager.run_action(
                "finder", "move", {"src": "~/Desktop/a.txt", "dst_dir": "~/Downloads"}
            )
        self.assertEqual(
            str(dir_ctx.exception), ERR_FINDER_DST_DIR_MISSING.format(dir=_res("~/Downloads"))
        )
        self.runner.register(FINDER_MOVE, Outcome(0, FINDER_DST_EXISTS_MARKER, ""))
        with self.assertRaises(ConnectorError) as exists_ctx:
            self.manager.run_action(
                "finder", "move", {"src": "~/Desktop/a.txt", "dst_dir": "~/Downloads"}
            )
        self.assertEqual(str(exists_ctx.exception), ERR_FINDER_DST_EXISTS.format(name="a.txt"))
        self.runner.register(FINDER_MOVE, Outcome(0, FINDER_NOT_FOUND_MARKER, ""))
        with self.assertRaises(ConnectorError) as missing_ctx:
            self.manager.run_action(
                "finder", "move", {"src": "~/Desktop/a.txt", "dst_dir": "~/Downloads"}
            )
        self.assertEqual(
            str(missing_ctx.exception), ERR_FINDER_NOT_FOUND.format(path=_res("~/Desktop/a.txt"))
        )
        # success stdout "moved": the builder post renders the move result
        self.runner.register(FINDER_MOVE, Outcome(0, "moved", ""))
        moved = self.manager.run_action(
            "finder", "move", {"src": "~/Desktop/a.txt", "dst_dir": "~/Downloads"}
        )
        self.assertEqual(
            moved,
            {"ok": True, "moved": True,
             "from": _res("~/Desktop/a.txt"),
             "to": os.path.join(_res("~/Downloads"), "a.txt")},
        )
        # pre-gate variant (mirror of the contacts validation seam): with
        # finder UNgranted, a bad relative src raises the validation copy
        # FIRST — the gate copy never appears and the runner is never invoked.
        # A fresh state dir keeps this manager's gate state unknown (setUp
        # granted finder in self.tmp).
        with tempfile.TemporaryDirectory() as gate_tmp:
            gate_runner = RecordingRunner()
            manager = ConnectorManager(gate_tmp, runner=gate_runner)
            self.assertEqual(manager.state.read()["finder"]["status"], "unknown")
            with self.assertRaises(ConnectorError) as pre_ctx:
                manager.run_action("finder", "move", {"src": "../x", "dst_dir": "~/Downloads"})
            self.assertEqual(
                str(pre_ctx.exception),
                "'src' must be an absolute path or begin with '~' — got '../x'",
            )
            self.assertEqual(gate_runner.calls, [])

    def test_path_validation_refusals(self):
        cases = [
            (("list", {"path": "/etc"}), ERR_PATH_OUTSIDE_ROOTS.format(path=_res("/etc"))),
            (("list", {"path": "relative/file.txt"}),
             "'path' must be an absolute path or begin with '~' — got 'relative/file.txt'"),
            (("list", {"path": "~/Desktop/../.."}),
             ERR_PATH_OUTSIDE_ROOTS.format(path=_res("~/Desktop/../.."))),
            (("reveal", {"path": "/etc"}), ERR_PATH_OUTSIDE_ROOTS.format(path=_res("/etc"))),
            (("info", {"path": "/etc"}), ERR_PATH_OUTSIDE_ROOTS.format(path=_res("/etc"))),
            (("move", {"src": "/etc", "dst_dir": "~/Downloads"}),
             ERR_PATH_OUTSIDE_ROOTS.format(path=_res("/etc"))),
            (("move", {"src": "~/Desktop/a.txt", "dst_dir": "/etc"}),
             ERR_PATH_OUTSIDE_ROOTS.format(path=_res("/etc"))),
            (("list", {"path": "~/Desktop/.."}),
             ERR_PATH_OUTSIDE_ROOTS.format(path=_res("~/Desktop/.."))),
            (("trash", {"path": "~"}), ERR_TRASH_HOME_REFUSED.format(path=_res("~"))),
            # expansion trick landing EXACTLY on the home root is still refused
            (("trash", {"path": "~/Desktop/.."}),
             ERR_TRASH_HOME_REFUSED.format(path=_res("~/Desktop/.."))),
            # two levels up lands ABOVE the home root (/Users): outside-roots
            (("trash", {"path": "~/Desktop/../.."}),
             ERR_PATH_OUTSIDE_ROOTS.format(path=_res("~/Desktop/../.."))),
            (("trash", {"path": "~/Desktop/\x00x"}), "'path' must be a non-empty path"),
        ]
        for (action, params), expected in cases:
            with self.assertRaises(ConnectorError) as ctx:
                self.manager.run_action("finder", action, params)
            self.assertEqual(str(ctx.exception), expected, (action, params))
        self.assertEqual(self.runner.calls, [])  # every refusal predates dispatch
        # trash outside the roots: a non-existent path refuses, a REAL plain
        # file passes validation and reaches the runner resolved
        with tempfile.TemporaryDirectory() as foreign_root:
            missing = os.path.join(foreign_root, "nao-existe.pdf")
            with self.assertRaises(ConnectorError) as outside_ctx:
                self.manager.run_action("finder", "trash", {"path": missing})
            self.assertEqual(
                str(outside_ctx.exception), ERR_PATH_OUTSIDE_ROOTS.format(path=_res(missing))
            )
            real_file = os.path.join(foreign_root, "forastero.txt")
            with open(real_file, "w", encoding="utf-8") as f:
                f.write("conteúdo")
            self.runner.register(FINDER_TRASH, Outcome(0, "trashed", ""))
            self.manager.run_action("finder", "trash", {"path": real_file})
            self.assertEqual(self.runner.last_argv(), [_res(real_file)])  # resolved temp path
        # the 513-char path hits _as_string BEFORE root validation
        with self.assertRaises(ConnectorError) as cap_ctx:
            self.manager.run_action("finder", "list", {"path": "p" * (FINDER_PATH_MAX_CHARS + 1)})
        self.assertEqual(
            str(cap_ctx.exception), f"'path' is limited to {FINDER_PATH_MAX_CHARS} characters"
        )

    def test_trash_is_never_a_void_delete(self):
        """Finder's `delete` MOVES TO TRASH (recoverable) — never a void
        delete: the frozen script carries `delete _item`, the constant comment
        documents the mission line, and an allowed path reaches the runner
        fully resolved (never a bare `/` or the home root)."""
        self.assertIn("delete _item", FINDER_TRASH)
        self.assertIn("MOVES TO TRASH", inspect.getsource(notifybot.connectors))
        self.runner.register(FINDER_TRASH, Outcome(0, "trashed", ""))
        result = self.manager.run_action("finder", "trash", {"path": "~/Desktop/trabalho"})
        resolved = _res("~/Desktop/trabalho")
        self.assertEqual(self.runner.last_argv(), [resolved])
        self.assertNotEqual(resolved, "/")
        self.assertNotEqual(resolved, os.path.realpath(os.path.expanduser("~")))
        self.assertEqual(result, {"ok": True, "trashed": resolved})

    def test_finder_injection_battery(self):
        """Model-controlled paths ride strictly as argv items against the
        FROZEN finder constants — never inside script source (the do-shell-
        script hygiene loop lives in the gating class, one home per the
        FASE-3 split)."""
        self.runner.register(FINDER_LIST, Outcome(0, "x", ""))
        self.runner.register(FINDER_MOVE, Outcome(0, "moved", ""))
        self.runner.register(FINDER_TRASH, Outcome(0, "trashed", ""))
        for payload in TestInjectionBattery.PAYLOADS:
            under_root = "~/Desktop/" + payload
            resolved = _res(under_root)
            self.runner.register(resolved, Outcome(0, "", ""))  # reveal: cmd[2] IS the path
            self.manager.run_action("finder", "reveal", {"path": under_root})
            self.assertEqual(self.runner.calls[-1][:1], ["open"], payload)
            self.assertEqual(self.runner.calls[-1][2:], [resolved], payload)
            # resolved argv items carry the payload verbatim (substring)
            self.assertIn(payload, self.runner.calls[-1][2], payload)
            self.manager.run_action("finder", "list", {"path": under_root})
            cmd = self.runner.calls[-1]
            self.assertEqual(cmd[2], FINDER_LIST, payload)
            self.assertTrue(
                any(payload in arg for arg in cmd[3:]), (payload, cmd[3:])
            )  # verbatim argv delivery
            self.assertNotIn(payload, cmd[2], payload)  # absent from source
            self.manager.run_action(
                "finder", "move", {"src": under_root, "dst_dir": "~/Downloads"}
            )
            cmd = self.runner.calls[-1]
            self.assertEqual(cmd[2], FINDER_MOVE, payload)
            self.assertTrue(
                any(payload in arg for arg in cmd[3:]), (payload, cmd[3:])
            )  # verbatim argv delivery
            self.assertNotIn(payload, cmd[2], payload)
            self.manager.run_action("finder", "trash", {"path": under_root})
            cmd = self.runner.calls[-1]
            self.assertEqual(cmd[2], FINDER_TRASH, payload)
            self.assertTrue(
                any(payload in arg for arg in cmd[3:]), (payload, cmd[3:])
            )  # verbatim argv delivery
            self.assertNotIn(payload, cmd[2], payload)
        for cmd in self.runner.calls:  # shape holds for EVERY payload
            if cmd[0] == "open":
                self.assertEqual(cmd[1], "-R", cmd)  # the argv-only reveal route
            else:
                self.assertEqual(cmd[:2], ["osascript", "-e"], cmd)


class TestQuickTimeDispatchActions(unittest.TestCase):
    """QuickTime dispatch: the single in-process recording slot (start /
    new_audio occupy it, stop releases it — always, even on doc-gone), the
    saved-path stop result, play's existing-file rule, and the memory-only
    restart semantics."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.runner = RecordingRunner()
        self.manager = ConnectorManager(self.tmp.name, runner=self.runner)
        self.manager.state.record("quicktime", "granted")

    def test_start_recording_states_and_slot(self):
        self.runner.register(QUICKTIME_START_SCREEN, Outcome(0, "DOC-1", ""))
        result = self.manager.run_action("quicktime", "start_recording", {"with_audio": True})
        self.assertEqual(result, {"ok": True, "doc_id": "DOC-1", "with_audio": True})
        self.assertIs(self.runner.calls[-1][2], QUICKTIME_START_SCREEN)
        self.assertEqual(self.runner.last_argv(), ["1"])
        self.assertEqual(self.manager._qt_recording, {"doc_id": "DOC-1", "kind": "screen"})
        # clear the slot through the stop flow between layouts
        self.runner.register(QUICKTIME_STOP, Outcome(0, "~/Movies/Screen Recording DOC-1.mov", ""))
        self.manager.run_action("quicktime", "stop_recording", {})
        self.assertIsNone(self.manager._qt_recording)
        # with_audio False rides "0"; absent params default to False
        result2 = self.manager.run_action("quicktime", "start_recording", {"with_audio": False})
        self.assertEqual(result2, {"ok": True, "doc_id": "DOC-1", "with_audio": False})
        self.assertEqual(self.runner.last_argv(), ["0"])
        self.manager.run_action("quicktime", "stop_recording", {})
        result3 = self.manager.run_action("quicktime", "start_recording", {})
        self.assertEqual(result3, {"ok": True, "doc_id": "DOC-1", "with_audio": False})
        self.assertEqual(self.runner.last_argv(), ["0"])
        # non-bool with_audio refuses at the builder (no runner trip)
        calls_before = list(self.runner.calls)
        with self.assertRaises(ConnectorError) as ctx:
            self.manager.run_action("quicktime", "start_recording", {"with_audio": "yes"})
        self.assertEqual(str(ctx.exception), "'with_audio' must be true or false")
        self.assertEqual(self.runner.calls, calls_before)

    def test_second_start_refused_one_at_a_time(self):
        self.runner.register(QUICKTIME_START_SCREEN, Outcome(0, "DOC-1", ""))
        self.manager.run_action("quicktime", "start_recording", {})
        calls_after_first = list(self.runner.calls)
        with self.assertRaises(ConnectorError) as ctx:
            self.manager.run_action("quicktime", "start_recording", {})
        self.assertEqual(str(ctx.exception), ERR_QT_ALREADY_RECORDING.format(doc_id="DOC-1"))
        self.assertEqual(self.runner.calls, calls_after_first)  # builder-level refusal

    def test_stop_recording_returns_saved_path_and_clears_slot(self):
        self.runner.register(QUICKTIME_START_SCREEN, Outcome(0, "DOC-1", ""))
        self.manager.run_action("quicktime", "start_recording", {})
        self.runner.register(
            QUICKTIME_STOP, Outcome(0, "~/Movies/Screen Recording DOC-1.mov", "")
        )
        result = self.manager.run_action("quicktime", "stop_recording", {})
        self.assertEqual(result, {"ok": True, "path": "~/Movies/Screen Recording DOC-1.mov"})
        self.assertEqual(self.runner.calls[-1][2:], [QUICKTIME_STOP, "DOC-1"])
        self.assertIsNone(self.manager._qt_recording)  # slot released
        with self.assertRaises(ConnectorError) as again_ctx:
            self.manager.run_action("quicktime", "stop_recording", {})
        self.assertEqual(str(again_ctx.exception), ERR_NO_RECORDING)

    def test_stop_doc_gone_marker_releases_slot(self):
        self.runner.register(QUICKTIME_START_SCREEN, Outcome(0, "DOC-7", ""))
        self.manager.run_action("quicktime", "start_recording", {})
        self.runner.register(QUICKTIME_STOP, Outcome(0, QUICKTIME_DOC_GONE_MARKER, ""))
        with self.assertRaises(ConnectorError) as ctx:
            self.manager.run_action("quicktime", "stop_recording", {})
        self.assertEqual(str(ctx.exception), ERR_QT_DOC_GONE.format(doc_id="DOC-7"))
        self.assertIsNone(self.manager._qt_recording)  # released FIRST, doc-gone or not

    def test_new_audio_recording_shares_slot(self):
        self.runner.register(QUICKTIME_NEW_AUDIO, Outcome(0, "DOC-2", ""))
        result = self.manager.run_action("quicktime", "new_audio_recording", {})
        self.assertEqual(result, {"ok": True, "doc_id": "DOC-2"})
        self.assertEqual(self.runner.calls[-1][2:], [QUICKTIME_NEW_AUDIO])  # no argv
        self.assertEqual(self.manager._qt_recording, {"doc_id": "DOC-2", "kind": "audio"})
        calls_after_first = list(self.runner.calls)
        with self.assertRaises(ConnectorError) as ctx:
            self.manager.run_action("quicktime", "new_audio_recording", {})
        self.assertEqual(str(ctx.exception), ERR_QT_ALREADY_RECORDING.format(doc_id="DOC-2"))
        self.assertEqual(self.runner.calls, calls_after_first)

    def test_play_requires_existing_file(self):
        with self.assertRaises(ConnectorError) as missing_ctx:
            self.manager.run_action("quicktime", "play", {"file": "~/Movies/missing.mov"})
        self.assertEqual(
            str(missing_ctx.exception), ERR_QT_PLAY_FILE.format(path=_res("~/Movies/missing.mov"))
        )
        with tempfile.TemporaryDirectory() as root:
            real_file = os.path.join(root, "demo.mov")
            with open(real_file, "w", encoding="utf-8") as f:
                f.write("x")
            self.runner.register(QUICKTIME_PLAY, Outcome(0, "0", ""))
            result = self.manager.run_action("quicktime", "play", {"file": real_file})
            self.assertEqual(result, {"ok": True, "path": _res(real_file)})
            self.assertEqual(self.runner.calls[-1][2:], [QUICKTIME_PLAY, _res(real_file)])
        with self.assertRaises(ConnectorError) as rel_ctx:
            self.manager.run_action("quicktime", "play", {"file": "missing.mov"})
        self.assertEqual(
            str(rel_ctx.exception),
            "'file' must be an absolute path or begin with '~' — got 'missing.mov'",
        )
        with self.assertRaises(ConnectorError) as cap_ctx:
            self.manager.run_action(
                "quicktime", "play", {"file": "p" * (QUICKTIME_FILE_MAX_CHARS + 1)}
            )
        self.assertEqual(
            str(cap_ctx.exception), f"'file' is limited to {QUICKTIME_FILE_MAX_CHARS} characters"
        )
        for bad_params in ({}, {"file": None}, {"file": ""}, {"file": "   "}):
            with self.assertRaises(ConnectorError) as empty_ctx:
                self.manager.run_action("quicktime", "play", bad_params)
            self.assertEqual(
                str(empty_ctx.exception), "'file' must be a non-empty string", bad_params
            )

    def test_cross_action_recording_conflicts_and_lifecycle(self):
        # 1. start_recording (screen) -> new_audio_recording (audio) raises conflict
        self.runner.register(QUICKTIME_START_SCREEN, Outcome(0, "DOC-SCREEN", ""))
        self.manager.run_action("quicktime", "start_recording", {})
        self.assertEqual(self.manager._qt_recording, {"doc_id": "DOC-SCREEN", "kind": "screen"})
        calls_after_start = list(self.runner.calls)
        with self.assertRaises(ConnectorError) as conflict_ctx:
            self.manager.run_action("quicktime", "new_audio_recording", {})
        self.assertEqual(
            str(conflict_ctx.exception), ERR_QT_ALREADY_RECORDING.format(doc_id="DOC-SCREEN")
        )
        self.assertEqual(self.runner.calls, calls_after_start)

        # clear screen recording slot via stop
        self.runner.register(QUICKTIME_STOP, Outcome(0, "~/Movies/Screen Recording DOC-SCREEN.mov", ""))
        self.manager.run_action("quicktime", "stop_recording", {})
        self.assertIsNone(self.manager._qt_recording)

        # 2. new_audio_recording (audio) -> stop_recording returns saved path and clears slot
        self.runner.register(QUICKTIME_NEW_AUDIO, Outcome(0, "DOC-AUDIO", ""))
        audio_res = self.manager.run_action("quicktime", "new_audio_recording", {})
        self.assertEqual(audio_res, {"ok": True, "doc_id": "DOC-AUDIO"})
        self.assertEqual(self.manager._qt_recording, {"doc_id": "DOC-AUDIO", "kind": "audio"})

        # (cross-check: audio active -> start_recording raises conflict)
        calls_after_audio = list(self.runner.calls)
        with self.assertRaises(ConnectorError) as audio_conflict_ctx:
            self.manager.run_action("quicktime", "start_recording", {})
        self.assertEqual(
            str(audio_conflict_ctx.exception), ERR_QT_ALREADY_RECORDING.format(doc_id="DOC-AUDIO")
        )
        self.assertEqual(self.runner.calls, calls_after_audio)

        self.runner.register(
            QUICKTIME_STOP, Outcome(0, "~/Movies/Audio Recording DOC-AUDIO.m4a", "")
        )
        stop_res = self.manager.run_action("quicktime", "stop_recording", {})
        self.assertEqual(stop_res, {"ok": True, "path": "~/Movies/Audio Recording DOC-AUDIO.m4a"})
        self.assertEqual(self.runner.calls[-1][2:], [QUICKTIME_STOP, "DOC-AUDIO"])
        self.assertIsNone(self.manager._qt_recording)

        # slot cleared: subsequent stop raises ERR_NO_RECORDING
        with self.assertRaises(ConnectorError) as no_rec_ctx:
            self.manager.run_action("quicktime", "stop_recording", {})
        self.assertEqual(str(no_rec_ctx.exception), ERR_NO_RECORDING)

    def test_recording_state_lost_on_restart(self):
        """The recording slot is process memory ONLY: the state file carries
        domain statuses only (nothing recording-shaped is persisted), and a
        fresh manager instance starts with a free slot (restart semantics)."""
        self.runner.register(QUICKTIME_START_SCREEN, Outcome(0, "DOC-1", ""))
        self.manager.run_action("quicktime", "start_recording", {})
        self.assertEqual(self.manager._qt_recording, {"doc_id": "DOC-1", "kind": "screen"})
        state_path = os.path.join(self.tmp.name, "data", "connectors_state.json")
        with open(state_path, "r", encoding="utf-8") as f:
            stored = json.load(f)
        self.assertTrue(set(stored) <= set(CONNECTOR_DOMAINS), sorted(stored))
        manager2 = ConnectorManager(self.tmp.name, runner=self.runner)
        self.assertIsNone(manager2._qt_recording)
        with self.assertRaises(ConnectorError) as ctx:
            manager2.run_action("quicktime", "stop_recording", {})
        self.assertEqual(str(ctx.exception), ERR_NO_RECORDING)


class TestFinderQuickTimeGatingProbeAndMockWorld(unittest.TestCase):
    """Membership batteries, gate machinery, probe classification, script
    hygiene, and the env-gated mock world observability for finder +
    quicktime."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_membership_and_gate_lists(self):
        self.assertIn("finder", CONNECTOR_DOMAINS)
        self.assertIn("quicktime", CONNECTOR_DOMAINS)
        self.assertEqual(CONNECTOR_DOMAINS.index("finder"), 8)
        self.assertEqual(CONNECTOR_DOMAINS.index("quicktime"), 9)
        self.assertEqual(CONNECTOR_DOMAINS[10], "system")
        self.assertIn("finder", GATED_DOMAINS)
        self.assertIn("quicktime", GATED_DOMAINS)
        self.assertEqual(
            CONNECTOR_ACTIONS["finder"], ("list", "reveal", "info", "move", "trash")
        )
        self.assertEqual(
            CONNECTOR_ACTIONS["quicktime"],
            ("start_recording", "stop_recording", "new_audio_recording", "play"),
        )

    def test_ungranted_gates_never_reach_runner_exact_copy(self):
        gate_runner = RecordingRunner()
        manager = ConnectorManager(self.tmp.name, runner=gate_runner)
        for domain, action, params, expected in (
            ("finder", "list", {"path": "~/Desktop"},
             "Finder ainda não está conectado — conecte finder em Settings › Connectors."),
            ("quicktime", "start_recording", {},
             "QuickTime ainda não está conectado — conecte quicktime em Settings › Connectors."),
        ):
            for status in ("unknown", "denied", "needs_consent"):
                manager.state.record(domain, status)
                self.assertEqual(gate_runner.calls, [])
                with self.assertRaises(ConnectorError) as ctx:
                    manager.run_action(domain, action, params)
                # gate copy flows from the existing PT-BR machinery verbatim
                self.assertEqual(str(ctx.exception), expected)
                self.assertEqual(gate_runner.calls, [])  # runner NEVER invoked through the gate

    def test_script_hygiene_marker_and_bundle_battery(self):
        self.assertIn("finder", PROBE_SCRIPTS)
        self.assertIn("quicktime", PROBE_SCRIPTS)
        for script in (FINDER_LIST, FINDER_INFO, FINDER_MOVE, FINDER_TRASH,
                       QUICKTIME_START_SCREEN, QUICKTIME_NEW_AUDIO,
                       QUICKTIME_STOP, QUICKTIME_PLAY):
            self.assertIn("on run argv", script)  # argv-only input channel
            self.assertNotIn("do shell script", script)
            self.assertNotIn("run script", script)
        for script in (FINDER_LIST, FINDER_INFO, FINDER_MOVE, FINDER_TRASH):
            self.assertIn("com.apple.finder", script)
        for script in (QUICKTIME_START_SCREEN, QUICKTIME_NEW_AUDIO,
                       QUICKTIME_STOP, QUICKTIME_PLAY):
            self.assertIn("com.apple.QuickTimePlayerX", script)
        self.assertIn(FINDER_NOT_FOUND_MARKER, FINDER_LIST)
        self.assertIn(FINDER_NOT_FOUND_MARKER, FINDER_INFO)
        self.assertIn(FINDER_NOT_FOUND_MARKER, FINDER_MOVE)
        self.assertIn(FINDER_NOT_FOUND_MARKER, FINDER_TRASH)
        self.assertIn(FINDER_DST_DIR_MISSING_MARKER, FINDER_MOVE)
        self.assertIn(FINDER_DST_EXISTS_MARKER, FINDER_MOVE)
        self.assertIn(QUICKTIME_DOC_GONE_MARKER, QUICKTIME_STOP)
        # probes: trivial consented touches that fire the TCC prompt
        self.assertIn("count of desktop items", PROBE_SCRIPTS["finder"])
        self.assertIn("com.apple.finder", PROBE_SCRIPTS["finder"])
        self.assertIn("count windows", PROBE_SCRIPTS["quicktime"])
        self.assertIn("com.apple.QuickTimePlayerX", PROBE_SCRIPTS["quicktime"])
        # SCRIPTS identity across all eight registered keys
        self.assertIs(SCRIPTS[("finder", "list")], FINDER_LIST)
        self.assertIs(SCRIPTS[("finder", "info")], FINDER_INFO)
        self.assertIs(SCRIPTS[("finder", "move")], FINDER_MOVE)
        self.assertIs(SCRIPTS[("finder", "trash")], FINDER_TRASH)
        self.assertIs(SCRIPTS[("quicktime", "start_recording")], QUICKTIME_START_SCREEN)
        self.assertIs(SCRIPTS[("quicktime", "new_audio_recording")], QUICKTIME_NEW_AUDIO)
        self.assertIs(SCRIPTS[("quicktime", "stop_recording")], QUICKTIME_STOP)
        self.assertIs(SCRIPTS[("quicktime", "play")], QUICKTIME_PLAY)
        # reveal is argv-only (`open -R`): NO SCRIPTS key may exist for it
        self.assertNotIn(("finder", "reveal"), SCRIPTS)

    def test_probe_classification_finder_and_quicktime(self):
        granted_runner = RecordingRunner()
        granted_runner.register(PROBE_SCRIPTS["finder"], Outcome(0, "2", ""))
        granted_runner.register(PROBE_SCRIPTS["quicktime"], Outcome(0, "0", ""))
        manager = ConnectorManager(self.tmp.name, runner=granted_runner)
        self.assertEqual(manager.probe("finder")["status"], "granted")
        self.assertEqual(manager.state.read()["finder"]["status"], "granted")
        self.assertEqual(manager.probe("quicktime")["status"], "granted")
        self.assertEqual(manager.state.read()["quicktime"]["status"], "granted")
        # canned -1743 denial: classified AND persisted (mirror the FASE-3
        # contacts/messages probe battery — -1743 is `denied`, never guessed)
        denied_runner = RecordingRunner()
        denied_runner.register(
            PROBE_SCRIPTS["finder"],
            Outcome(1, "", "Not authorized to send Apple events (-1743)"),
        )
        denied_runner.register(
            PROBE_SCRIPTS["quicktime"],
            Outcome(1, "", "Not authorized to send Apple events (-1743)"),
        )
        manager2 = ConnectorManager(self.tmp.name, runner=denied_runner)
        self.assertEqual(manager2.probe("finder")["status"], "denied")
        self.assertEqual(manager2.state.read()["finder"]["status"], "denied")
        self.assertEqual(manager2.probe("quicktime")["status"], "denied")
        self.assertEqual(manager2.state.read()["quicktime"]["status"], "denied")
        # a hang classifies needs_consent (persisted) — runner-seam split
        manager2.state.record("finder", "unknown")
        manager2.state.record("quicktime", "unknown")

        def timeout_runner(cmd, timeout, stdin=None):
            raise subprocess.TimeoutExpired(cmd, timeout)

        manager2._runner = timeout_runner
        self.assertEqual(manager2.probe("finder")["status"], "needs_consent")
        self.assertEqual(manager2.state.read()["finder"]["status"], "needs_consent")
        self.assertEqual(manager2.probe("quicktime")["status"], "needs_consent")
        self.assertEqual(manager2.state.read()["quicktime"]["status"], "needs_consent")

    def test_single_param_actions_require_non_empty_string(self):
        """Single-param actions (finder.reveal, finder.info, finder.trash,
        quicktime.play) refuse missing / None / empty string parameters
        before any gate or runner execution."""
        runner = RecordingRunner()
        manager = ConnectorManager(self.tmp.name, runner=runner)
        manager.state.record("finder", "granted")
        manager.state.record("quicktime", "granted")
        matrix = [
            ("finder", "reveal", "path"),
            ("finder", "info", "path"),
            ("finder", "trash", "path"),
            ("quicktime", "play", "file"),
        ]
        for domain, action, param in matrix:
            for bad_params in ({}, {param: None}, {param: ""}, {param: "   "}):
                with self.assertRaises(ConnectorError) as ctx:
                    manager.run_action(domain, action, bad_params)
                self.assertEqual(
                    str(ctx.exception),
                    f"'{param}' must be a non-empty string",
                    (domain, action, bad_params),
                )
        self.assertEqual(runner.calls, [])

    def test_mock_observability_finder_and_quicktime(self):
        """Env-mock world: seeded finder folders observed through list
        (Desktop, Downloads), a file-path list refusal mirror, the dst-exists
        refusal against the seeded duplicate, a REAL move -> Downloads list
        shows it, trash -> Desktop list shrinks, a missing trash target and a
        mock-missing dst_dir both refuse with the exact copies; quicktime runs
        start -> stop with sequential QT-MOCK ids, the single-slot refusal,
        slot release, and a real-file play."""
        try:
            os.environ["NOTIFYBOT_CONNECTORS_MOCK"] = "1"
        finally:
            pass
        try:
            manager = ConnectorManager(self.tmp.name)  # env mock runner active
            manager.state.record("finder", "granted")
            manager.state.record("quicktime", "granted")
            _desktop = _res("~/Desktop")
            _downloads = _res("~/Downloads")
            # seeded folders verbatim
            listed = manager.run_action("finder", "list", {"path": "~/Desktop"})
            self.assertEqual(
                listed, {"items": ["Relatório.pdf", "Captura de Tela.png", "trabalho"]}
            )
            self.assertEqual(
                manager.run_action("finder", "list", {"path": "~/Downloads"}),
                {"items": ["Relatório.pdf"]},
            )
            # listing a FILE path mirrors the script's failed folder coercion
            with self.assertRaises(ConnectorError) as no_folder_ctx:
                manager.run_action("finder", "list", {"path": "~/Desktop/Relatório.pdf"})
            self.assertEqual(
                str(no_folder_ctx.exception),
                ERR_FINDER_NO_FOLDER.format(path=os.path.join(_desktop, "Relatório.pdf")),
            )
            # seeded Downloads duplicate: the frozen dst-exists refusal mirrors
            with self.assertRaises(ConnectorError) as dup_ctx:
                manager.run_action(
                    "finder", "move",
                    {"src": "~/Desktop/Relatório.pdf", "dst_dir": "~/Downloads"},
                )
            self.assertEqual(
                str(dup_ctx.exception), ERR_FINDER_DST_EXISTS.format(name="Relatório.pdf")
            )
            # real observability: move -> list shows it in Downloads
            moved = manager.run_action(
                "finder", "move",
                {"src": "~/Desktop/Captura de Tela.png", "dst_dir": "~/Downloads"},
            )
            self.assertEqual(
                moved,
                {"ok": True, "moved": True,
                 "from": os.path.join(_desktop, "Captura de Tela.png"),
                 "to": os.path.join(_downloads, "Captura de Tela.png")},
            )
            self.assertEqual(
                manager.run_action("finder", "list", {"path": "~/Downloads"})["items"],
                ["Relatório.pdf", "Captura de Tela.png"],
            )
            # trash -> list observes the removal
            trashed = manager.run_action("finder", "trash", {"path": "~/Desktop/trabalho"})
            self.assertEqual(trashed, {"ok": True, "trashed": os.path.join(_desktop, "trabalho")})
            self.assertEqual(
                manager.run_action("finder", "list", {"path": "~/Desktop"})["items"],
                ["Relatório.pdf"],
            )
            # trash of a MISSING under-roots path -> the marker's clean copy
            with self.assertRaises(ConnectorError) as missing_ctx:
                manager.run_action("finder", "trash", {"path": "~/Desktop/fantasma.txt"})
            self.assertEqual(
                str(missing_ctx.exception),
                ERR_FINDER_NOT_FOUND.format(path=os.path.join(_desktop, "fantasma.txt")),
            )
            # dst_dir outside the MOCK world (but under allowed roots): the
            # frozen dst-dir-missing refusal mirrors
            with self.assertRaises(ConnectorError) as dst_ctx:
                manager.run_action(
                    "finder", "move",
                    {"src": "~/Desktop/Relatório.pdf", "dst_dir": "~/Documents"},
                )
            self.assertEqual(
                str(dst_ctx.exception), ERR_FINDER_DST_DIR_MISSING.format(dir=_res("~/Documents"))
            )
            # quicktime: start -> single-slot refusal -> stop returns the mock
            # saved path EXACTLY and clears the slot; start again gets the NEXT
            # sequential id; new_audio shares the same slot; play rides a real file
            started = manager.run_action("quicktime", "start_recording", {})
            self.assertEqual(started, {"ok": True, "doc_id": "QT-MOCK-1", "with_audio": False})
            with self.assertRaises(ConnectorError) as second_ctx:
                manager.run_action("quicktime", "start_recording", {})
            self.assertEqual(
                str(second_ctx.exception), ERR_QT_ALREADY_RECORDING.format(doc_id="QT-MOCK-1")
            )
            with self.assertRaises(ConnectorError) as cross_audio_ctx:
                manager.run_action("quicktime", "new_audio_recording", {})
            self.assertEqual(
                str(cross_audio_ctx.exception), ERR_QT_ALREADY_RECORDING.format(doc_id="QT-MOCK-1")
            )
            stopped = manager.run_action("quicktime", "stop_recording", {})
            self.assertEqual(stopped, {"ok": True, "path": "~/Movies/Screen Recording QT-MOCK-1.mov"})
            with self.assertRaises(ConnectorError) as no_rec_ctx:
                manager.run_action("quicktime", "stop_recording", {})
            self.assertEqual(str(no_rec_ctx.exception), ERR_NO_RECORDING)
            started2 = manager.run_action("quicktime", "start_recording", {})
            self.assertEqual(started2, {"ok": True, "doc_id": "QT-MOCK-2", "with_audio": False})
            manager.run_action("quicktime", "stop_recording", {})
            audio = manager.run_action("quicktime", "new_audio_recording", {})
            self.assertEqual(audio, {"ok": True, "doc_id": "QT-MOCK-3"})
            with self.assertRaises(ConnectorError) as audio_ctx:
                manager.run_action("quicktime", "new_audio_recording", {})
            self.assertEqual(
                str(audio_ctx.exception), ERR_QT_ALREADY_RECORDING.format(doc_id="QT-MOCK-3")
            )
            with self.assertRaises(ConnectorError) as cross_screen_ctx:
                manager.run_action("quicktime", "start_recording", {})
            self.assertEqual(
                str(cross_screen_ctx.exception), ERR_QT_ALREADY_RECORDING.format(doc_id="QT-MOCK-3")
            )
            stopped_audio = manager.run_action("quicktime", "stop_recording", {})
            self.assertEqual(
                stopped_audio, {"ok": True, "path": "~/Movies/Screen Recording QT-MOCK-3.mov"}
            )
            self.assertIsNone(manager._qt_recording)
            play_path = os.path.join(self.tmp.name, "demo.mov")
            with open(play_path, "w", encoding="utf-8") as f:
                f.write("x")
            played = manager.run_action("quicktime", "play", {"file": play_path})
            self.assertEqual(played, {"ok": True, "path": _res(play_path)})
        finally:
            os.environ.pop("NOTIFYBOT_CONNECTORS_MOCK", None)

    def test_injection_battery_finder_quicktime(self):
        """Model-controlled strings ride strictly as argv items against the
        FROZEN finder/quicktime constants — never inside script source.
        quicktime play needs an existing file: each payload embeds in a real
        tempfile's name where the OS permits; payloads containing `/` cannot
        name a real file, so those assert play's existing-file refusal."""
        runner = RecordingRunner()
        runner.register(FINDER_LIST, Outcome(0, "x", ""))
        runner.register(FINDER_MOVE, Outcome(0, "moved", ""))
        runner.register(FINDER_TRASH, Outcome(0, "trashed", ""))
        runner.register(QUICKTIME_PLAY, Outcome(0, "0", ""))
        manager = ConnectorManager(self.tmp.name, runner=runner)
        manager.state.record("finder", "granted")
        manager.state.record("quicktime", "granted")
        for payload in TestInjectionBattery.PAYLOADS:
            under_root = "~/Desktop/" + payload
            manager.run_action("finder", "list", {"path": under_root})
            cmd = runner.calls[-1]
            script_source = cmd[2]
            self.assertEqual(script_source, FINDER_LIST, payload)
            self.assertTrue(
                any(payload in arg for arg in cmd[3:]), (payload, cmd[3:])
            )  # verbatim argv delivery
            self.assertNotIn(payload, script_source, payload)  # absent from source
            manager.run_action("finder", "move", {"src": under_root, "dst_dir": "~/Downloads"})
            cmd = runner.calls[-1]
            script_source = cmd[2]
            self.assertEqual(script_source, FINDER_MOVE, payload)
            self.assertTrue(
                any(payload in arg for arg in cmd[3:]), (payload, cmd[3:])
            )  # verbatim argv delivery
            self.assertNotIn(payload, script_source, payload)
            manager.run_action("finder", "trash", {"path": under_root})
            cmd = runner.calls[-1]
            script_source = cmd[2]
            self.assertEqual(script_source, FINDER_TRASH, payload)
            self.assertTrue(
                any(payload in arg for arg in cmd[3:]), (payload, cmd[3:])
            )  # verbatim argv delivery
            self.assertNotIn(payload, script_source, payload)
            candidate = os.path.join(self.tmp.name, payload)
            if "/" not in payload:
                real_file = os.path.join(self.tmp.name, "play-" + payload)
                with open(real_file, "w", encoding="utf-8") as f:
                    f.write("x")
                manager.run_action("quicktime", "play", {"file": real_file})
                cmd = runner.calls[-1]
                self.assertEqual(cmd[2], QUICKTIME_PLAY, payload)
                self.assertIn(payload, cmd[3], payload)  # verbatim argv delivery (resolved path)
                self.assertNotIn(payload, cmd[2], payload)  # absent from source
            else:
                with self.assertRaises(ConnectorError) as play_ctx:
                    manager.run_action("quicktime", "play", {"file": candidate})
                self.assertEqual(
                    str(play_ctx.exception),
                    ERR_QT_PLAY_FILE.format(path=_res(candidate)),
                    payload,
                )
        for cmd in runner.calls:  # shape holds for EVERY payload
            self.assertEqual(cmd[:2], ["osascript", "-e"])


if __name__ == "__main__":
    unittest.main()