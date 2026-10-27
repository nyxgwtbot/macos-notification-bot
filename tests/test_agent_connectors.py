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

    def register(self, script, outcome):
        self.outcomes[script] = outcome

    def __call__(self, cmd, timeout):
        self.calls.append(list(cmd))
        self.timeout_seen = timeout
        script = cmd[2] if len(cmd) > 2 else ""
        for key, outcome in self.outcomes.items():
            if script == key:
                return outcome
        raise AssertionError("unexpected script handed to fake runner: %r" % script[:60])

    def last_argv(self):
        return self.calls[-1][3:] if self.calls else None


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

        injected = ConnectorManager(self.tmp.name, runner=lambda cmd, t: Outcome(0, "Finder", ""))
        self.registry.connector_manager = injected
        dispatched = self.registry.dispatch("native.call", {"domain": "system", "action": "front_app", "params": {}})
        self.assertEqual(dispatched, {"app": "Finder"})

    def test_connector_error_maps_to_tool_error_result(self):
        from notifybot.connectors import ConnectorManager

        # gate error (PT-BR) must surface VERBATIM as the tool error result.
        self.registry.connector_manager = ConnectorManager(
            self.tmp.name, runner=lambda cmd, t: Outcome(0, "", "")
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

    def test_volume_get_set_front_app_toast(self):
        self.runner.register(SCRIPTS[("system", "volume")], Outcome(0, "37", ""))
        self.assertEqual(self.manager.run_action("system", "volume", {}), {"level": 37})
        self.runner.register(SCRIPTS[("system", "volume_set")], Outcome(0, "42", ""))
        self.assertEqual(self.manager.run_action("system", "volume", {"level": 42}), {"ok": True, "level": 42})
        self.assertEqual(self.runner.last_argv(), ["42"])
        # string levels coerce; echo the requested level back for the assert
        self.manager._runner = lambda cmd, t: Outcome(0, cmd[3], "")
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

        def open_runner(cmd, timeout):
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
        self.manager = ConnectorManager(self.tmp.name, runner=lambda cmd, t: Outcome(0, "", ""))

    def test_timeout_maps_to_ptbr_error_mentioning_prompt(self):
        def timeout_runner(cmd, timeout):
            raise subprocess.TimeoutExpired(cmd, timeout)

        self.manager._runner = timeout_runner
        self.manager.state.record("reminders", "granted")
        with self.assertRaises(ConnectorError) as ctx:
            self.manager.run_action("reminders", "add", {"list": "L", "title": "t"})
        self.assertIn("consentimento", str(ctx.exception))
        self.assertIn("Settings › Connectors", str(ctx.exception))

    def test_deny_maps_to_ptbr_error_and_persists_denied(self):
        def deny_runner(cmd, timeout):
            return Outcome(1, "", "script error: Not authorized to send Apple events (-1743)")

        self.manager._runner = deny_runner
        self.manager.state.record("notes", "granted")
        with self.assertRaises(ConnectorError) as ctx:
            self.manager.run_action("notes", "read", {"name": "X"})
        self.assertIn("-1743", str(ctx.exception))
        self.assertIn("Settings › Connectors", str(ctx.exception))
        self.assertEqual(self.manager.state.read()["notes"]["status"], "denied")

    def test_other_stderr_maps_to_english_trimmed_error(self):
        def boom_runner(cmd, timeout):
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

        def sneaky_runner(cmd, timeout):
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

        def timeout_runner(cmd, timeout):
            raise subprocess.TimeoutExpired(cmd, timeout)

        manager._runner = timeout_runner
        self.assertEqual(manager.probe("reminders")["status"], "needs_consent")
        self.assertEqual(manager.state.read()["reminders"]["status"], "needs_consent")

        manager._runner = lambda cmd, timeout: Outcome(
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
            self.assertIn("on run argv", source, key)
        # Every app script addresses the app by bundle id (pt-BR locale-proof).
        self.assertIn("com.apple.reminders", SCRIPTS[("reminders", "list_lists")])
        self.assertIn("com.apple.Notes", SCRIPTS[("notes", "search")])
        self.assertIn("com.apple.systemevents", SCRIPTS[("system", "front_app")])

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
            manager = ConnectorManager(root, runner=lambda cmd, t: Outcome(0, "0", ""))
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
            manager = ConnectorManager(root, runner=lambda cmd, t: Outcome(0, "0", ""))
            rows = manager.state.read()
            self.assertEqual(
                {domain: rows[domain]["status"] for domain in CONNECTOR_DOMAINS},
                {"reminders": "unknown", "notes": "unknown", "system": "unknown"},
            )

    def test_single_flight_probe_busy_then_free(self):
        with tempfile.TemporaryDirectory() as root:
            manager = ConnectorManager(root, runner=lambda cmd, t: Outcome(0, "0", ""))
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
        self.manager = ConnectorManager(self.tmp.name, runner=lambda cmd, t: Outcome(0, "0", ""))
        self.fixture = ApiServerFixture(valid_config_payload())
        self.addCleanup(self.fixture.close)
        # Inject the mockable manager into the booted runtime (lazy singleton slot).
        self.fixture.httpd.runtime.connector_manager = self.manager

    def test_get_connectors_shape_and_system_granted(self):
        status, body = self.fixture.request("GET", "/api/connectors")
        self.assertEqual(status, 200)
        connectors = body["connectors"]
        self.assertEqual(set(connectors), {"reminders", "notes", "system"})
        for domain in ("reminders", "notes"):
            row = connectors[domain]
            self.assertEqual(row["status"], "unknown")
            self.assertFalse(row["granted"])
            self.assertIsNone(row["last_probe"])
            self.assertIn("consent prompt", row["hint"])
        self.assertEqual(
            connectors["system"],
            {"status": "granted", "granted": True, "last_probe": None, "hint": "no consent required"},
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
            def __call__(self, cmd, timeout):
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

    def __call__(self, cmd, timeout):
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


if __name__ == "__main__":
    unittest.main()