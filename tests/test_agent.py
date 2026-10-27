#!/usr/bin/env python3
"""Unit tests for the in-app agent loop + tools + API rows 11-14.

Mirrors tests/test_api.py style: the server boots on port 0 in a thread
against a tempfile.TemporaryDirectory fixture repo and the artifacts
sub-service registry is constructed with port 0 (NOTIFYBOT_ARTIFACTS_PORT=0)
so returned urls carry the real bound port — tests never bind 4770. The LLM
function is mocked everywhere: unit tests inject scripted transcripts via
run_turn(..., llm_fn=ScriptedLLM(...)); the API-level tests flip
NOTIFYBOT_SMOKE_AGENT_MOCK=1 (the documented smoke hook) before any request.
"""

import json
import os
import re
import tempfile
import unittest
import urllib.request

from tests.test_api import ApiServerFixture, valid_config_payload
from notifybot.agent import (
    AgentSessionStore,
    build_context,
    parse_model_json,
    run_turn,
    truncate_tool_result_text,
)
from notifybot.agent_tools import (
    ARTIFACT_MAX_FILES,
    ArtifactServerRegistry,
    ToolRegistry,
    prune_artifacts,
)
from notifybot.api import BotApiRuntime

URL_RE = re.compile(r"^http://127\.0\.0\.1:(\d+)/([0-9a-f]{32})\.html$")
DASHBOARD_HTML = (
    "<!DOCTYPE html><html><head><title>Health Dashboard</title></head>"
    "<body><h1>Health Dashboard</h1><p>ok</p></body></html>"
)


class ScriptedLLM:
    """Queued model outputs: str entries verbatim, callables receive the
    messages list. Records every call for exact call-count assertions."""

    def __init__(self, steps):
        self.steps = list(steps)
        self.calls = []

    def __call__(self, messages):
        self.calls.append(messages)
        if not self.steps:
            return json.dumps({"answer": "fallback answer", "urls": []})
        step = self.steps.pop(0)
        return step(messages) if callable(step) else step

    @property
    def call_count(self):
        return len(self.calls)


# ---- message/transcript helpers ------------------------------------------------


def find_last_tool_result(messages, tool_name):
    """Content of the newest `tool_result:` line under a tool_call of
    `tool_name`, or {} (mirrors the transcript line format in agent.py)."""
    for message in reversed(messages):
        content = str(message.get("content", ""))
        if message.get("role") != "assistant" or not content.startswith("tool_call:"):
            continue
        try:
            call = json.loads(content.splitlines()[0][len("tool_call:") :])
        except (ValueError, IndexError):
            continue
        if tool_name is not None and call.get("tool") != tool_name:
            continue
        for line in content.splitlines():
            if line.startswith("tool_result:"):
                try:
                    return json.loads(line[len("tool_result:") :])
                except ValueError:
                    continue
    return {}


# ---- runtime factory ------------------------------------------------------------


def make_runtime(tmpdir):
    """BotApiRuntime against a tempdir config + port-0 artifacts registry."""
    config_path = os.path.join(tmpdir, "config.json")
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(valid_config_payload(), f)
    runtime = BotApiRuntime(config_path=config_path)
    runtime.agent_registry = ArtifactServerRegistry(runtime.artifacts_dir(), port=0)
    runtime.tool_registry = ToolRegistry(runtime, runtime.agent_registry)
    return runtime


# ---- loop-level (direct run_turn) -----------------------------------------------


class TestDashboardChainSuccess(unittest.TestCase):
    """Full tool-chain: app.data -> dashboard.create -> server.serve -> answer."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.runtime = make_runtime(self.tmp.name)
        self.artifacts_dir = self.runtime.artifacts_dir()

    def test_full_pipeline(self):
        llm = ScriptedLLM(
            [
                json.dumps({"tool": "app.data", "args": {"dataset": "health"}}),
                json.dumps(
                    {
                        "tool": "dashboard.create",
                        "args": {
                            "title": "Health Dashboard",
                            "html": DASHBOARD_HTML,
                            "data_source": "app.data:health",
                        },
                    }
                ),
                lambda messages: json.dumps(
                    {
                        "tool": "server.serve",
                        "args": {
                            "artifact_id": find_last_tool_result(messages, "dashboard.create")["artifact_id"]
                        },
                    }
                ),
                lambda messages: json.dumps(
                    {
                        "answer": "Aqui está o dashboard do bot, pronto na aba Chat.",
                        "urls": [find_last_tool_result(messages, "server.serve")["url"]],
                    }
                ),
            ]
        )
        response = run_turn(self.runtime, None, "me mostre um dashboard, por favor", llm_fn=llm)

        self.assertEqual([step["tool"] for step in response["turn"]],
                         ["app.data", "dashboard.create", "server.serve"])
        self.assertEqual([step["role"] for step in response["turn"]], ["assistant"] * 3)
        self.assertTrue(all(step.get("error") is None for step in response["turn"]))
        for step in response["turn"]:
            self.assertIn("ts", step)

        self.assertEqual(llm.call_count, 4)
        self.assertTrue(response["answer"])
        self.assertEqual(len(response["urls"]), 1)
        match = URL_RE.match(response["urls"][0])
        self.assertIsNotNone(match, response["urls"])
        artifact_id = match.group(2)

        # Artifact file exists on disk and is served over real HTTP.
        artifact_path = os.path.join(self.artifacts_dir, f"{artifact_id}.html")
        self.assertTrue(os.path.isfile(artifact_path))
        with urllib.request.urlopen(response["urls"][0], timeout=5) as resp:
            body = resp.read().decode("utf-8")
        self.assertEqual(resp.status, 200)
        self.assertIn("text/html", resp.headers.get("Content-Type", ""))
        self.assertIn("<title>Health Dashboard</title>", body)

        # Session persisted: user + 3 tool steps + answer.
        records = AgentSessionStore(self.runtime.config_dir).load(response["session_id"])
        self.assertIsNotNone(records)
        self.assertEqual([r.get("role") for r in records],
                         ["user", "assistant", "assistant", "assistant", "assistant"])
        self.assertIn("answer", records[-1])
        self.assertEqual(records[-1]["urls"], response["urls"])
        server_status = self.runtime.agent_registry.list()
        self.assertTrue(server_status["running"])
        self.assertEqual(server_status["port"], int(match.group(1)))

    def tearDown(self):
        if self.runtime.agent_registry is not None:
            self.runtime.agent_registry.stop()


class TestMaxTurnsGuard(unittest.TestCase):
    """7 queued tool calls -> exactly 6 executed + PT-BR guard answer."""

    def test_guard_after_six_turns(self):
        with tempfile.TemporaryDirectory() as root:
            runtime = make_runtime(root)
            llm = ScriptedLLM(
                [json.dumps({"tool": "app.data", "args": {"dataset": "health"}}) for _ in range(7)]
            )
            response = run_turn(runtime, None, "spam de health", llm_fn=llm)
            self.assertEqual(llm.call_count, 6)
            self.assertEqual(len(response["turn"]), 6)
            self.assertTrue(all(step["tool"] == "app.data" for step in response["turn"]))
            self.assertIn("atingi o limite", response["answer"])
            self.assertIn("6 passos", response["answer"])
            records = AgentSessionStore(runtime.config_dir).load(response["session_id"])
            self.assertEqual(len(records), 8)  # user + 6 steps + guard answer


class TestUnparsableRetry(unittest.TestCase):
    """Garbage output -> one protocol-reminder retry -> answer; 2 LLM calls."""

    def test_garbage_then_answer(self):
        with tempfile.TemporaryDirectory() as root:
            runtime = make_runtime(root)
            llm = ScriptedLLM(
                [
                    "olha, não sei — vou responder em texto livre com markdown e **prosa**",
                    json.dumps({"answer": "Olá! Como posso ajudar hoje?", "urls": []}),
                ]
            )
            response = run_turn(runtime, None, "ping", llm_fn=llm)
            self.assertEqual(llm.call_count, 2)
            # The retry context carried the protocol reminder (not persisted).
            retry_messages = llm.calls[1]
            self.assertIn(
                "EXACTLY ONE JSON object",
                retry_messages[-1]["content"],
            )
            self.assertEqual(response["turn"], [])
            self.assertEqual(response["answer"], "Olá! Como posso ajudar hoje?")
            records = AgentSessionStore(runtime.config_dir).load(response["session_id"])
            self.assertEqual([r.get("role") for r in records], ["user", "assistant"])
            self.assertNotIn("EXACTLY ONE JSON object", records[0]["content"])

    def test_two_failures_error_answer_and_session_kept(self):
        with tempfile.TemporaryDirectory() as root:
            runtime = make_runtime(root)
            llm = ScriptedLLM(["sem formato", "ainda sem formato"])
            response = run_turn(runtime, None, "ping", llm_fn=llm)
            self.assertEqual(llm.call_count, 2)
            self.assertIn("formato JSON", response["answer"])
            records = AgentSessionStore(runtime.config_dir).load(response["session_id"])
            self.assertIsNotNone(records)
            self.assertEqual(records[-1]["answer"], response["answer"])


class TestLLMFailureGraceful(unittest.TestCase):
    """Ollama failure -> graceful PT-BR error answer + session file on disk."""

    def test_llm_fn_raises_timeout(self):
        with tempfile.TemporaryDirectory() as root:
            runtime = make_runtime(root)

            def timeout_llm(messages):
                raise TimeoutError("simulated ollama hang")

            response = run_turn(runtime, None, "status?", llm_fn=timeout_llm)
            self.assertEqual(response["turn"], [])
            self.assertTrue(response["answer"])
            self.assertIn("Ollama", response["answer"])
            records = AgentSessionStore(runtime.config_dir).load(response["session_id"])
            self.assertIsNotNone(records)
            self.assertEqual(records[-1]["answer"], response["answer"])

    def test_unknown_session_is_not_found(self):
        with tempfile.TemporaryDirectory() as root:
            runtime = make_runtime(root)
            with self.assertRaises(ValueError) as ctx:
                run_turn(runtime, "00000000-0000-0000-0000-000000000000", "ping", llm_fn=ScriptedLLM([]))
            self.assertIn("not found", str(ctx.exception))


# ---- tool-level -----------------------------------------------------------------


class TestDashboardValidationRejection(unittest.TestCase):
    """dashboard.create rejects scripts, external refs, oversize HTML, and
    over-long titles — tool error results, loop continues."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.runtime = make_runtime(self.tmp.name)

    def dispatch_dashboard(self, html, title="x"):
        return self.runtime.tool_registry.dispatch(
            "dashboard.create", {"title": title, "html": html, "data_source": "test"}
        )

    def test_script_and_external_refs_rejected(self):
        cases = [
            '<html><body><script>alert(1)</script></body></html>',
            '<html><body><a href="https://example.com">external</a></body></html>',
            '<html><body><img src="http://track.example/pixel"></body></html>',
            "<html><head><link rel=\"stylesheet\" href=\"x\"></head><body></body></html>",
            "<html><body><style>@import 'http://x';</style></body></html>",
            "<html><body><style>div{background:url(http://x)}</style></body></html>",
            "<html><body><script>fetch('http://x')</script></body></html>",
            "<html><body><script>new XMLHttpRequest()</script></body></html>",
            "<html><body><a href='https://single-quote.com'>x</a></body></html>",
        ]
        for html in cases:
            result = self.dispatch_dashboard(html)
            self.assertIn("error", result, html)
            self.assertIn("rejected", result["error"], html)
        # Nothing was written to the artifacts dir.
        self.assertFalse(os.path.isdir(self.runtime.artifacts_dir()))

    def test_oversize_html_rejected(self):
        big = "<!DOCTYPE html><html><body>" + "x" * 512_100 + "</body></html>"
        result = self.dispatch_dashboard(big)
        self.assertIn("error", result)
        self.assertIn("512", result["error"])

    def test_invalid_args_rejected(self):
        result = self.dispatch_dashboard(DASHBOARD_HTML, title="y" * 121)
        self.assertIn("error", result)
        result = self.runtime.tool_registry.dispatch("dashboard.create", {"title": "x"})
        self.assertIn("error", result)

    def test_unknown_tool_and_unknown_dataset_are_error_results(self):
        unknown_tool = self.runtime.tool_registry.dispatch("does.not", {})
        self.assertIn("error", unknown_tool)
        self.assertIn("unknown tool", unknown_tool["error"])
        bad_dataset = self.runtime.tool_registry.dispatch("app.data", {"dataset": "nope"})
        self.assertIn("error", bad_dataset)
        self.assertIn("unknown dataset", bad_dataset["error"])
        good = self.runtime.tool_registry.dispatch("app.data", {"dataset": "health"})
        self.assertEqual(good["ok"], True)

    def test_app_data_events_dataset_via_existing_handler(self):
        result = self.runtime.tool_registry.dispatch(
            "app.data", {"dataset": "events", "params": {"limit": 5}}
        )
        self.assertEqual(result["count"], 0)  # empty fixture log


class TestRegistryIdempotency(unittest.TestCase):
    """ensure×2 -> ONE server instance/port; stop -> list consistent."""

    def test_ensure_idempotent_and_stop(self):
        with tempfile.TemporaryDirectory() as root:
            registry = ArtifactServerRegistry(os.path.join(root, "artifacts"), port=0)
            try:
                first = registry.ensure()
                server_first = registry._server
                second = registry.ensure()
                self.assertIs(registry._server, server_first)
                self.assertEqual(first["url"], second["url"])

                status = registry.list()
                self.assertTrue(status["running"])
                self.assertNotEqual(status["port"], 0)
                self.assertEqual(status["port"], int(re.search(r":(\d+)/", first["url"]).group(1)))

                registry.stop()
                self.assertFalse(registry.list()["running"])
                self.assertIsNone(registry.list()["since"])

                # ensure after stop restarts (idempotent lifecycle contract);
                # with an ephemeral port the new bind may pick a new port.
                third = registry.ensure()
                match = re.search(r"^http://127\.0\.0\.1:(\d+)/$", third["url"])
                self.assertIsNotNone(match, third["url"])
                self.assertEqual(registry.list()["port"], int(match.group(1)))
            finally:
                registry.stop()

    def test_ensure_missing_artifact_is_error(self):
        with tempfile.TemporaryDirectory() as root:
            registry = ArtifactServerRegistry(os.path.join(root, "artifacts"), port=0)
            try:
                with self.assertRaises(Exception) as ctx:
                    registry.ensure("0" * 32)
                self.assertIn("artifact not found", str(ctx.exception))
            finally:
                registry.stop()


class TestArtifactPruning(unittest.TestCase):
    """Rotation guard: cap 200, newest kept (mtime sort), oldest removed."""

    def test_prune_keeps_newest_200(self):
        with tempfile.TemporaryDirectory() as root:
            for index in range(ARTIFACT_MAX_FILES + 5):
                path = os.path.join(root, f"{index:032x}.html")
                with open(path, "w", encoding="utf-8") as f:
                    f.write("<html></html>")
                os.utime(path, (1_000_000 + index, 1_000_000 + index))
            removed = prune_artifacts(root)
            self.assertEqual(removed, 5)
            remaining = sorted(name for name in os.listdir(root) if name.endswith(".html"))
            self.assertEqual(len(remaining), ARTIFACT_MAX_FILES)
            # The five oldest (index 0..4) are gone.
            self.assertNotIn(f"{0:032x}.html", remaining)
            self.assertIn(f"{ARTIFACT_MAX_FILES + 4:032x}.html", remaining)


# ---- context + protocol helpers --------------------------------------------------


class TestProtocolParsing(unittest.TestCase):
    def test_accepts_fenced_tool_json(self):
        parsed = parse_model_json('```json\n{"tool": "app.data", "args": {"dataset": "health"}}\n```')
        self.assertEqual(parsed, {"tool": "app.data", "args": {"dataset": "health"}})

    def test_accepts_prose_wrapped_answer(self):
        parsed = parse_model_json('Claro! {"answer": "olá", "urls": ["http://127.0.0.1:4770/a.html"]} até!')
        self.assertEqual(parsed, {"answer": "olá", "urls": ["http://127.0.0.1:4770/a.html"]})

    def test_rejects_non_protocol_shapes(self):
        for raw in [None, "", "   ", "garbage", "[]", '{"foo": 1}', '{"tool": ""}', '{"tool": 123}',
                    '{"answer": ""}', '{"answer": null}']:
            self.assertIsNone(parse_model_json(raw))

    def test_answer_without_urls_is_empty_list(self):
        self.assertEqual(parse_model_json('{"answer": "ok"}'), {"answer": "ok", "urls": []})


class TestContextWindowAndTruncation(unittest.TestCase):
    def test_window_keeps_last_eight_messages(self):
        prompt = build_context("SYS", [{"role": "user", "content": f"m{i}"} for i in range(12)])
        self.assertEqual(prompt[0]["role"], "system")
        self.assertEqual(prompt[0]["content"], "SYS")
        bodies = [message["content"] for message in prompt[1:]]
        self.assertEqual(bodies, [f"m{i}" for i in range(4, 12)])

    def test_tool_step_renders_transcript_lines(self):
        records = [
            {"role": "user", "content": "ping"},
            {"role": "assistant", "tool": "app.data", "args": {"dataset": "health"}, "result": {"ok": True}, "ts": "t"},
        ]
        rendered = build_context("SYS", records)
        tool_message = rendered[-1]
        self.assertEqual(tool_message["role"], "assistant")
        self.assertIn("tool_call:", tool_message["content"])
        self.assertIn("tool_result:", tool_message["content"])
        self.assertIn('"dataset": "health"', tool_message["content"])

    def test_truncation_caps_at_4k_with_marker(self):
        payload = json.dumps({"items": ["x" * 100] * 100})
        trimmed = truncate_tool_result_text(payload)
        self.assertLessEqual(len(trimmed.encode("utf-8")), 4096)
        self.assertIn("truncated", trimmed)
        self.assertIn("persisted in the session file", trimmed)
        # untouched when small
        self.assertEqual(truncate_tool_result_text('{"ok": true}'), '{"ok": true}')


# ---- API-level (rows 11-14 over HTTP, mock-mode LLM) -----------------------------


class AgentApiServerFixture(ApiServerFixture):
    """ApiServerFixture with port-0 artifacts registry injection (the port env
    is consumed lazily by ensure_agent_registry on the first agent request)."""

    def __init__(self, config, **kwargs):
        os.environ["NOTIFYBOT_ARTIFACTS_PORT"] = "0"
        super().__init__(config, **kwargs)

    def close(self):
        registry = getattr(self.httpd.runtime, "agent_registry", None)
        if registry is not None:
            registry.stop()
        os.environ.pop("NOTIFYBOT_ARTIFACTS_PORT", None)
        super().close()


class TestAgentApiEndpoints(unittest.TestCase):
    """#11-#14 over HTTP: 400s, sessions flows, /artifacts 404 on the API port."""

    @classmethod
    def setUpClass(cls):
        os.environ["NOTIFYBOT_SMOKE_AGENT_MOCK"] = "1"
        cls.fixture = AgentApiServerFixture(valid_config_payload())

    @classmethod
    def tearDownClass(cls):
        cls.fixture.close()
        os.environ.pop("NOTIFYBOT_SMOKE_AGENT_MOCK", None)

    def test_agent_turn_400s(self):
        for body in [{}, {"message": ""}, {"message": "   "}, {"message": "x" * 9000},
                     {"message": ["nope"]}, None]:
            if body is None:
                # no JSON body at all
                status, error_body = self.fixture.request("POST", "/api/agent/turn", None)
            else:
                status, error_body = self.fixture.request("POST", "/api/agent/turn", body)
            self.assertEqual(status, 400, body)
            self.assertIn("error", error_body)

    def test_agent_turn_session_flow_and_routes(self):
        status, body = self.fixture.request("POST", "/api/agent/turn", {"message": "Olá bot! Tudo bem?"})
        self.assertEqual(status, 200)
        session_id = body["session_id"]
        self.assertRegex(session_id, r"^[A-Za-z0-9-]{1,64}$")
        self.assertEqual(body["turn"], [])  # mock answers directly, no tools
        self.assertTrue(body["answer"])
        self.assertEqual(body["urls"], [])

        # #12 sessions list shows the new session with a preview
        status, listing = self.fixture.request("GET", "/api/agent/sessions")
        self.assertEqual(status, 200)
        sessions = listing["sessions"]
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0]["session_id"], session_id)
        self.assertTrue(sessions[0]["preview"].startswith("Olá bot!"))
        self.assertEqual(sessions[0]["turns"], 1)
        self.assertTrue(sessions[0]["updated_at"])

        # #13 full session fetch
        status, detail = self.fixture.request("GET", f"/api/agent/session/{session_id}")
        self.assertEqual(status, 200)
        self.assertEqual(detail["session_id"], session_id)
        roles = [record["role"] for record in detail["records"]]
        self.assertEqual(roles, ["user", "assistant"])
        self.assertEqual(detail["records"][0]["content"], "Olá bot! Tudo bem?")
        self.assertEqual(detail["records"][1]["answer"], body["answer"])

        # Same message again within the session keeps history (2 turns).
        status, followup = self.fixture.request(
            "POST", "/api/agent/turn", {"session_id": session_id, "message": "me mostre um dashboard"}
        )
        self.assertEqual(status, 200)
        self.assertEqual(followup["session_id"], session_id)
        self.assertTrue(followup["answer"])
        # Mock-mode full pipeline over HTTP: served artifact URL works end to end.
        self.assertTrue(followup["turn"], "expected tool steps from the dashboard pipeline")
        self.assertEqual(len(followup["urls"]), 1)
        match = URL_RE.match(followup["urls"][0])
        self.assertIsNotNone(match, followup["urls"])
        with urllib.request.urlopen(followup["urls"][0], timeout=5) as served:
            self.assertIn("<title>Agent Health Overview</title>", served.read().decode("utf-8"))
        status, listing = self.fixture.request("GET", "/api/agent/sessions")
        self.assertEqual(listing["sessions"][0]["turns"], 2)

    def test_session_route_400_and_404(self):
        status, body = self.fixture.request("GET", "/api/agent/session/bad$id")
        self.assertEqual(status, 400)
        status, body = self.fixture.request("GET", "/api/agent/session/00000000-0000-0000-0000-000000000000")
        self.assertEqual(status, 404)
        self.assertIn("not found", body["error"])
        status, body = self.fixture.request(
            "POST", "/api/agent/turn", {"session_id": "00000000-0000-0000-0000-000000000000", "message": "ping"}
        )
        self.assertEqual(status, 404)

    def test_artifacts_path_is_404_on_api_port(self):
        # The artifacts sub-service lives on 4770, never on the API port.
        status, body = self.fixture.request("GET", "/artifacts/deadbeef.html")
        self.assertEqual(status, 404)
        self.assertIn("error", body)

    def test_services_row_shape(self):
        status, body = self.fixture.request("GET", "/api/services")
        self.assertEqual(status, 200)
        artifacts = body["services"]["artifacts"]
        for key in ("running", "port", "root", "since"):
            self.assertIn(key, artifacts)
        # The registry was started by the dashboard-pipeline test in this
        # class (unittest orders methods alphabetically): row must be truthful.
        self.assertTrue(artifacts["running"])
        self.assertGreater(artifacts["port"], 0)
        self.assertTrue(artifacts["since"])
        self.assertTrue(
            artifacts["root"].endswith(os.path.join("", "artifacts"))
            and artifacts["root"].endswith("artifacts")
        )


if __name__ == "__main__":
    unittest.main()