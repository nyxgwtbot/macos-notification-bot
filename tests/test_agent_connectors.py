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
        connectors = body["connectors"]
        self.assertEqual(
            set(connectors),
            {"reminders", "notes", "calendar", "music", "shortcuts", "mail", "system"},
        )
        for domain in ("reminders", "notes", "calendar", "music", "mail"):
            row = connectors[domain]
            self.assertEqual(row["status"], "unknown")
            self.assertFalse(row["granted"])
            self.assertIsNone(row["last_probe"])
            self.assertIn("consent prompt", row["hint"])
        self.assertEqual(
            connectors["system"],
            {"status": "granted", "granted": True, "last_probe": None, "hint": "no consent required"},
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
        self.assertEqual(CONNECTOR_DOMAINS[6], "system")
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
            {"reminders", "notes", "calendar", "music", "shortcuts", "mail", "system"},
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


if __name__ == "__main__":
    unittest.main()