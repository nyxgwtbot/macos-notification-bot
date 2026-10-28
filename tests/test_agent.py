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
import unittest.mock
import urllib.error
import urllib.request
from datetime import datetime

from tests.test_api import ApiServerFixture, valid_config_payload
from notifybot import agent as agent_module
from notifybot.agent import (
    AUTO_COMPLETE_PHASE,
    AUTO_SERVE_PHASE,
    AgentSessionStore,
    PRIOR_ANSWER_INLINE_MAX_CHARS,
    PRIOR_ANSWER_KEPT_CHARS,
    RAW_MODEL_OUTPUT_MAX_BYTES,
    REMINDER_PREFIX,
    STANDING_PROTOCOL_REMINDER,
    build_context,
    continuation_reminder_for,
    current_date_ground,
    grounded_system_prompt,
    load_system_prompt,
    parse_model_json,
    run_turn,
    summarize_tool_args,
    synthesize_dashboard_answer,
    truncate_prior_answer_text,
    truncate_raw_model_output,
    truncate_tool_result_text,
)
from notifybot.agent_tools import (
    ARTIFACT_MAX_FILES,
    ArtifactServerRegistry,
    ToolRegistry,
    prune_artifacts,
)
from notifybot.api import BotApiRuntime
from notifybot.finance import FinanceLedger

URL_RE = re.compile(r"^http://127\.0\.0\.1:(\d+)/([0-9a-f]{32})\.html$")
DASHBOARD_HTML = (
    "<!DOCTYPE html><html><head><title>Health Dashboard</title></head>"
    "<body><h1>Health Dashboard</h1><p>ok</p></body></html>"
)
# Large-but-valid dashboard body (> TOOL_ARG_INLINE_MAX_CHARS) whose unique
# marker must never surface back into the model context after redaction.
DRIFT_HTML_SENTINEL = "SENTINEL-BODY-7c4f"
LARGE_HTML = (
    "<!DOCTYPE html><html><head><title>Finance Dashboard</title></head>"
    f"<body><h1>Finance</h1><p>{DRIFT_HTML_SENTINEL * 12}</p></body></html>"
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
    """Full tool-chain: app.data -> dashboard.create -> [loop auto_serve] ->
    answer (the model never calls server.serve itself anymore)."""

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
        # The serve step is LOOP-executed: phase auto_serve, no model slot spent.
        self.assertEqual(response["turn"][2].get("phase"), AUTO_SERVE_PHASE)

        self.assertEqual(llm.call_count, 3)
        self.assertTrue(response["answer"])
        self.assertEqual(len(response["urls"]), 1)
        match = URL_RE.match(response["urls"][0])
        self.assertIsNotNone(match, response["urls"])
        artifact_id = match.group(2)

        # The model's answer step got the finalize recipe (real served URL).
        finalize_tail = llm.calls[2][-1]
        self.assertTrue(finalize_tail["content"].startswith(REMINDER_PREFIX))
        self.assertIn('"urls": ["' + response["urls"][0] + '"]', finalize_tail["content"])

        # Artifact file exists on disk and is served over real HTTP.
        artifact_path = os.path.join(self.artifacts_dir, f"{artifact_id}.html")
        self.assertTrue(os.path.isfile(artifact_path))
        with urllib.request.urlopen(response["urls"][0], timeout=5) as resp:
            body = resp.read().decode("utf-8")
        self.assertEqual(resp.status, 200)
        self.assertIn("text/html", resp.headers.get("Content-Type", ""))
        self.assertIn("<title>Health Dashboard</title>", body)

        # Session persisted: user + [(raw row per LLM call), tool step] for the
        # two model tool turns, the loop auto_serve step (NO raw row before it),
        # then (raw row for the final answer call) + answer.
        records = AgentSessionStore(self.runtime.config_dir).load(response["session_id"])
        self.assertIsNotNone(records)
        self.assertEqual([r.get("role") for r in records],
                         ["user", "assistant_raw", "assistant",
                          "assistant_raw", "assistant",
                          "assistant",
                          "assistant_raw", "assistant"])
        self.assertEqual(records[5].get("phase"), AUTO_SERVE_PHASE)
        self.assertEqual(records[5]["tool"], "server.serve")
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
            self.assertEqual(len(records), 14)  # user + 6×(raw, step) + guard answer


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
            self.assertEqual([r.get("role") for r in records],
                             ["user", "assistant_raw", "assistant_raw", "assistant"])
            self.assertNotIn("EXACTLY ONE JSON object", records[0]["content"])
            # Both drift outputs were persisted as raw rows (post-mortem data).
            self.assertEqual([r["phase"] for r in records[1:3]], ["attempt", "protocol_retry"])
            self.assertIn("markdown", records[1]["raw_model_output"])
            # The retry raw row holds the exact escaped JSON the model produced.
            self.assertIn("Como posso ajudar hoje?", records[2]["raw_model_output"])

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


class TestDriftAfterCreateGuard(unittest.TestCase):
    """Regression (real run 8f0ae727): after dashboard.create ok the model
    produced TWO unparsable outputs even with re-anchoring — previously this
    killed the pipeline and the user never got the URL. With the loop
    executing server.serve itself (auto_serve), the turn now COMPLETES with
    a synthesized PT-BR answer (auto_complete) carrying the served URL."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.runtime = make_runtime(self.tmp.name)
        self.addCleanup(self.runtime.agent_registry.stop)

    def test_drift_after_create_synthesizes_completed_pipeline(self):
        llm = ScriptedLLM(
            [
                json.dumps(
                    {
                        "tool": "dashboard.create",
                        "args": {"title": "Finance Month", "html": LARGE_HTML, "data_source": "app.data:finance_summary"},
                    }
                ),
                "Já montei o dashboard financeiro deste mês, ele ficou pronto! 🎉",
                "Vou exibir o dashboard agora na aba do aplicativo para você.",
            ]
        )
        response = run_turn(self.runtime, None, "Crie um dashboard financeiro deste mês", llm_fn=llm)
        self.assertEqual(llm.call_count, 3)

        # Pipeline completed WITHOUT a model answer: turn = create + loop
        # auto_serve; the synthesized answer delivers the served URL.
        self.assertEqual([step["tool"] for step in response["turn"]], ["dashboard.create", "server.serve"])
        self.assertEqual(response["turn"][1].get("phase"), AUTO_SERVE_PHASE)
        self.assertEqual(response["turn"][1]["args"].get("artifact_id"),
                         response["turn"][0]["result"]["artifact_id"])
        self.assertEqual(response["phase"], AUTO_COMPLETE_PHASE)
        self.assertIn("Dashboard pronto:", response["answer"])
        self.assertEqual(len(response["urls"]), 1)
        match = URL_RE.match(response["urls"][0])
        self.assertIsNotNone(match, response["urls"])
        with urllib.request.urlopen(response["urls"][0], timeout=5) as resp:
            body = resp.read().decode("utf-8")
        self.assertEqual(resp.status, 200)
        self.assertIn("<title>Finance Dashboard</title>", body)

        # The artifact was persisted and remains on disk.
        artifact_id = response["turn"][0]["result"]["artifact_id"]
        self.assertTrue(os.path.isfile(os.path.join(self.runtime.artifacts_dir(), f"{artifact_id}.html")))

        # Both post-create attempts (initial + protocol retry) were
        # re-anchored with the FINALIZE recipe: the real served URL is in the
        # prompt, and the model is told NOT to call any tool — NOT the old
        # server.serve args template.
        for tail in (llm.calls[1][-1], llm.calls[2][-1]):
            self.assertEqual(tail["role"], "user")
            self.assertTrue(tail["content"].startswith(REMINDER_PREFIX))
            self.assertIn('"urls": ["' + response["urls"][0] + '"]', tail["content"])
            self.assertNotIn('"args": {"action": "ensure"', tail["content"])

        # Session roles: user, raw(create), create, auto_serve serve (NO raw
        # row before it — no model call), raw(attempt), raw(protocol_retry),
        # synthesized answer — drift outputs persisted for post-mortem.
        records = AgentSessionStore(self.runtime.config_dir).load(response["session_id"])
        self.assertEqual([r.get("role") for r in records],
                         ["user", "assistant_raw", "assistant", "assistant",
                          "assistant_raw", "assistant_raw", "assistant"])
        raw_rows = [r for r in records if r.get("role") == "assistant_raw"]
        self.assertEqual([r["phase"] for r in raw_rows], ["attempt", "attempt", "protocol_retry"])
        self.assertIn("Já montei", raw_rows[1]["raw_model_output"])
        self.assertIn("Vou exibir", raw_rows[2]["raw_model_output"])
        self.assertEqual(records[3].get("phase"), AUTO_SERVE_PHASE)
        self.assertEqual(records[-1]["phase"], AUTO_COMPLETE_PHASE)
        self.assertEqual(records[-1]["answer"], response["answer"])
        self.assertEqual(records[-1]["urls"], response["urls"])

    def test_create_failure_no_completion_guard_answer_kept(self):
        """Without a completed create->serve pipeline the hard-failure guard
        text is untouched: bad create -> drift x2 -> PT-BR JSON-guard answer."""
        llm = ScriptedLLM(
            [
                json.dumps(
                    {
                        "tool": "dashboard.create",
                        "args": {
                            "title": "Broke",
                            # forbidden pattern -> tool error
                            "html": "<!DOCTYPE html><html><body><script>x()</script></body></html>",
                            "data_source": "app.data:finance_summary",
                        },
                    }
                ),
                "Dashboard criado!",  # drift 1
                "Tudo certo por aqui.",  # drift 2
            ]
        )
        response = run_turn(self.runtime, None, "Crie um dashboard", llm_fn=llm)
        self.assertEqual(llm.call_count, 3)
        self.assertIn("formato JSON", response["answer"])
        self.assertEqual(response["urls"], [])
        self.assertEqual([step for step in response["turn"] if step["tool"] == "server.serve"], [])
        self.assertNotIn("phase", response)


class TestRecoveryAfterCreate(unittest.TestCase):
    """Recovery path: unparsable output once after the loop's auto_serve
    step, then the finalize recipe reminder leads the model to the final
    answer -> full URL works."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.runtime = make_runtime(self.tmp.name)
        self.addCleanup(self.runtime.agent_registry.stop)

    def test_one_drift_output_recovers_via_finalize_recipe(self):
        llm = ScriptedLLM(
            [
                json.dumps(
                    {
                        "tool": "dashboard.create",
                        "args": {"title": "Finance Month", "html": LARGE_HTML, "data_source": "app.data:finance_summary"},
                    }
                ),
                "Pronto! O dashboard financeiro deste mês já está disponível.",  # drift attempt 1
                lambda messages: json.dumps(
                    {
                        "answer": "Aqui está o dashboard financeiro deste mês, pronto na aba Chat.",
                        "urls": [find_last_tool_result(messages, "server.serve")["url"]],
                    }
                ),
            ]
        )
        response = run_turn(self.runtime, None, "Crie um dashboard financeiro deste mês", llm_fn=llm)
        self.assertEqual(llm.call_count, 3)
        self.assertEqual([step["tool"] for step in response["turn"]], ["dashboard.create", "server.serve"])
        self.assertEqual(response["turn"][1].get("phase"), AUTO_SERVE_PHASE)
        self.assertEqual(response["answer"], "Aqui está o dashboard financeiro deste mês, pronto na aba Chat.")

        # answer.urls[0] works end-to-end (HTTP GET 200 on the artifact).
        match = URL_RE.match(response["urls"][0])
        self.assertIsNotNone(match, response["urls"])
        with urllib.request.urlopen(response["urls"][0], timeout=5) as resp:
            body = resp.read().decode("utf-8")
        self.assertEqual(resp.status, 200)
        self.assertIn("<title>Finance Dashboard</title>", body)

        # First attempt got the finalize continuation; the re-anchoring retry
        # re-used the same finalize recipe (real url, no tool request).
        self.assertTrue(llm.calls[1][-1]["content"].startswith(REMINDER_PREFIX))
        retry_tail = llm.calls[2][-1]
        self.assertTrue(retry_tail["content"].startswith(REMINDER_PREFIX))
        self.assertIn('"urls": ["' + response["urls"][0] + '"]', retry_tail["content"])
        self.assertNotIn('"args": {"action": "ensure"', retry_tail["content"])

        records = AgentSessionStore(self.runtime.config_dir).load(response["session_id"])
        self.assertEqual([r.get("role") for r in records],
                         ["user", "assistant_raw", "assistant",
                          "assistant", "assistant_raw", "assistant_raw", "assistant"])
        self.assertEqual(records[3].get("phase"), AUTO_SERVE_PHASE)


class TestPostCreatePromptHygiene(unittest.TestCase):
    """Regression (c): the post-create outgoing prompt must NOT contain the
    raw HTML body (args redacted to args_summary + artifact_hint) while the
    create tool_result row stays intact; full args remain in the session file
    and in the current-turn execution path only."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.runtime = make_runtime(self.tmp.name)
        self.addCleanup(self.runtime.agent_registry.stop)

    def test_post_create_prompt_redacts_html_and_finalizes(self):
        llm = ScriptedLLM(
            [
                json.dumps(
                    {
                        "tool": "dashboard.create",
                        "args": {"title": "Finance Month", "html": LARGE_HTML, "data_source": "app.data:finance_summary"},
                    }
                ),
                lambda messages: json.dumps(
                    {
                        "answer": "Dashboard financeiro pronto.",
                        "urls": [find_last_tool_result(messages, "server.serve")["url"]],
                    }
                ),
            ]
        )
        response = run_turn(self.runtime, None, "me mostre o dashboard financeiro", llm_fn=llm)
        self.assertEqual(llm.call_count, 2)

        # The post-create outgoing prompt: no raw HTML body, markers present.
        post_create_dump = json.dumps(llm.calls[1], ensure_ascii=False)
        self.assertNotIn(DRIFT_HTML_SENTINEL, post_create_dump)
        create_call_message = [m for m in llm.calls[1] if '"args_summary"' in str(m.get("content"))][0]
        self.assertIn("tool_call:", create_call_message["content"])
        self.assertIn("stored in the session file, not re-sent", create_call_message["content"])
        # The create RESULT row (small) stays intact and carries the artifact id.
        artifact_id = find_last_tool_result(llm.calls[1], "dashboard.create")["artifact_id"]
        self.assertTrue(artifact_id)
        # The loop's auto_serve step (with the served URL) is in the same
        # prompt; quotes in the raw content are never escaped there.
        serve_message = [m for m in llm.calls[1] if str(m.get("content")).startswith("tool_call:")][-1]
        self.assertIn('"tool": "server.serve"', serve_message["content"])
        self.assertIn('"url": "http://127.0.0.1:', serve_message["content"])

        # The finalize reminder carries the exact final-answer template with
        # the REAL served URL substituted.
        tail = llm.calls[1][-1]
        self.assertTrue(tail["content"].startswith(REMINDER_PREFIX))
        self.assertIn('"urls": ["' + response["urls"][0] + '"]', tail["content"])
        self.assertNotIn('"args": {"action": "ensure"', tail["content"])

        # Full args persist in the session file (post-mortem) and in the
        # current-turn execution path/response payload.
        records = AgentSessionStore(self.runtime.config_dir).load(response["session_id"])
        create_record = [r for r in records if r.get("tool") == "dashboard.create"][0]
        self.assertIn(DRIFT_HTML_SENTINEL, create_record["args"]["html"])
        self.assertIn(DRIFT_HTML_SENTINEL, response["turn"][0]["args"]["html"])
        auto_serve_record = [r for r in records if r.get("tool") == "server.serve"][0]
        self.assertEqual(auto_serve_record.get("phase"), AUTO_SERVE_PHASE)

        # End-to-end: the answer's url serves the artifact (200).
        with urllib.request.urlopen(response["urls"][0], timeout=5) as resp:
            self.assertEqual(resp.status, 200)


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


# ---- date grounding (prompt header) ----------------------------------------------


class TestDateGrounding(unittest.TestCase):
    """The system prompt header carries a build-time English date line, and a
    model that reads it issues CURRENT-month data calls."""

    def test_current_date_ground_line_format_deterministic(self):
        line = current_date_ground(datetime(2026, 10, 27, 9, 30, 15))
        self.assertEqual(line, "Current local date: 2026-10-27 Tuesday (2026-10).")
        self.assertEqual(current_date_ground(datetime(2026, 1, 3)), "Current local date: 2026-01-03 Saturday (2026-01).")

    def test_current_date_ground_uses_local_now_and_english_weekday(self):
        line = current_date_ground()
        match = re.fullmatch(
            r"Current local date: (\d{4}-\d{2}-\d{2}) ([A-Za-z]+) \((\d{4}-\d{2})\)\.",
            line,
        )
        self.assertIsNotNone(match, line)
        from notifybot.agent import _WEEKDAYS_EN

        self.assertIn(match.group(2), _WEEKDAYS_EN)  # always an English weekday name
        local_now = datetime.now()
        self.assertEqual(match.group(1), local_now.strftime("%Y-%m-%d"))
        self.assertEqual(match.group(3), local_now.strftime("%Y-%m"))

    def test_grounded_system_prompt_header(self):
        prompt = grounded_system_prompt()
        self.assertTrue(prompt.startswith("Current local date: "))
        self.assertIn(load_system_prompt(), prompt)

    def test_run_turn_context_carries_date_line(self):
        with tempfile.TemporaryDirectory() as root:
            runtime = make_runtime(root)
            llm = ScriptedLLM([])  # fallback answer, no tools
            run_turn(runtime, None, "ping", llm_fn=llm)
            system_message = llm.calls[0][0]
            self.assertEqual(system_message["role"], "system")
            self.assertIn("Current local date: ", system_message["content"])
            self.assertRegex(system_message["content"], r"Current local date: \d{4}-\d{2}-\d{2} \w+ \(\d{4}-\d{2}\)\.")

    def test_scripted_transcript_grounds_current_month_query(self):
        """Regression (real run): the model queried finance_transactions for
        the WRONG month ("deste mês" == current). With the date-grounded
        header, a script that reads the date line queries the CURRENT month
        and the tool returns the seeded current-month row."""
        with tempfile.TemporaryDirectory() as root:
            runtime = make_runtime(root)
            local_now = datetime.now()
            current_month = local_now.strftime("%Y-%m")
            ledger = FinanceLedger(db_path=os.path.join(runtime.config_dir, "finance.db"))
            ledger.insert_transaction(
                {
                    "rec_id": 901,
                    "ts": f"{current_month}-05T12:00:00-03:00",
                    "bundle": "com.bank",
                    "amount": 123.45,
                    "kind": "gasto",
                    "category": "alimentacao",
                    "description": "compra do mes corrente",
                    "raw": {},
                }
            )
            ledger.close()

            def month_from_date_line(messages):
                match = re.search(r"Current local date: (\d{4}-\d{2}-\d{2})", messages[0]["content"])
                assert match, messages[0]
                month = match.group(1)[:7]
                return json.dumps(
                    {
                        "tool": "app.data",
                        "args": {"dataset": "finance_transactions", "params": {"month": month}},
                    }
                )

            def answer_from_result(messages):
                result = find_last_tool_result(messages, "app.data")
                records = result.get("records", [])
                assert records, result  # the current-month row must be there
                return json.dumps(
                    {
                        "answer": f"Encontrei {len(records)} transações no mês {result.get('month')}.",
                        "urls": [],
                    }
                )

            llm = ScriptedLLM([month_from_date_line, answer_from_result])
            response = run_turn(runtime, None, "transações deste mês", llm_fn=llm)
            self.assertEqual(response["answer"], f"Encontrei 1 transações no mês {current_month}.")
            data_step = response["turn"][0]
            self.assertEqual(data_step["tool"], "app.data")
            self.assertEqual(data_step["args"]["params"]["month"], current_month)
            self.assertEqual(data_step["result"]["month"], current_month)
            self.assertEqual(len(data_step["result"]["records"]), 1)
            self.assertEqual(data_step["result"]["records"][0]["description"], "compra do mes corrente")


# ---- answer synthesis ------------------------------------------------------------


class TestSynthesizeDashboardAnswer(unittest.TestCase):
    """Pure helper: create+serve results -> the PT-BR auto_complete answer."""

    def test_shape_and_delivered_url(self):
        create_result = {"artifact_id": "ab" * 16, "path": "/artifacts/ab.html"}
        served_url = "http://127.0.0.1:4770/" + "ab" * 16 + ".html"
        synth = synthesize_dashboard_answer(create_result, {"url": served_url})
        self.assertEqual(set(synth), {"answer", "urls"})
        self.assertEqual(synth["urls"], [served_url])
        self.assertEqual(synth["urls"][0], served_url)  # urls[0] == the served URL
        self.assertIn("Dashboard pronto:", synth["answer"])
        self.assertIn(served_url, synth["answer"])
        # Internal artifact bookkeeping never leaks into the answer text.
        self.assertNotIn("artifact_id", synth)
        self.assertNotIn("ab" + "ab", synth["answer"].replace(served_url, ""))

    def test_missing_url_degrades_to_empty_urls(self):
        synth = synthesize_dashboard_answer({"artifact_id": "x"}, {})
        self.assertEqual(synth["urls"], [])
        self.assertTrue(synth["answer"])
        self.assertNotIn("http", synth["answer"])
        # Malformed/None inputs degrade the same way (never raises).
        self.assertEqual(synthesize_dashboard_answer(None, None)["urls"], [])
        self.assertEqual(synthesize_dashboard_answer({}, {"url": None})["urls"], [])


# ---- Ollama transport resilience --------------------------------------------------


class TestOllamaTransportRetry(unittest.TestCase):
    """_ollama_chat retries transient transport errors (2 extra attempts, 2 s
    linear backoff) at the _transport_post seam; exhaustion keeps the
    run_turn guard path untouched."""

    def setUp(self):
        os.environ.pop("NOTIFYBOT_SMOKE_AGENT_MOCK", None)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.runtime = make_runtime(self.tmp.name)
        self.messages = [{"role": "system", "content": "system prompt"}]

    def test_two_transport_failures_then_success(self):
        post_mock = unittest.mock.patch.object(
            agent_module,
            "_transport_post",
            side_effect=[
                urllib.error.URLError("connection refused #1"),
                urllib.error.URLError("connection refused #2"),
                "hello model",
            ],
        )
        backoff_mock = unittest.mock.patch.object(agent_module, "_transport_backoff")
        with post_mock as posts, backoff_mock as sleeps:
            result = agent_module._ollama_chat(self.runtime, self.messages)
        self.assertEqual(result, "hello model")
        self.assertEqual(posts.call_count, 3)  # 1 attempt + 2 retries
        self.assertEqual(sleeps.call_args_list, [unittest.mock.call(1), unittest.mock.call(2)])

    def test_socket_timeout_retried_then_succeeds(self):
        with unittest.mock.patch.object(
            agent_module,
            "_transport_post",
            side_effect=[TimeoutError("simulated hang"), "ok after timeout"],
        ) as posts, unittest.mock.patch.object(agent_module, "_transport_backoff"):
            self.assertEqual(agent_module._ollama_chat(self.runtime, self.messages), "ok after timeout")
        self.assertEqual(posts.call_count, 2)

    def test_http_error_5xx_retried_4xx_not(self):
        http_502 = urllib.error.HTTPError("http://127.0.0.1:11434/api/chat", 502, "Bad Gateway", {}, None)
        http_404 = urllib.error.HTTPError("http://127.0.0.1:11434/api/chat", 404, "Not Found", {}, None)
        with unittest.mock.patch.object(agent_module, "_transport_post", side_effect=http_502) as posts_5xx, \
                unittest.mock.patch.object(agent_module, "_transport_backoff"):
            with self.assertRaises(urllib.error.HTTPError):
                agent_module._ollama_chat(self.runtime, self.messages)
            self.assertEqual(posts_5xx.call_count, 3)
        with unittest.mock.patch.object(agent_module, "_transport_post", side_effect=http_404) as posts_4xx:
            with self.assertRaises(urllib.error.HTTPError):
                agent_module._ollama_chat(self.runtime, self.messages)
            self.assertEqual(posts_4xx.call_count, 1)  # deterministic error: NO retry

    def test_three_consecutive_transport_failures_raise(self):
        with unittest.mock.patch.object(
            agent_module,
            "_transport_post",
            side_effect=[urllib.error.URLError("down")] * 3,
        ) as posts, unittest.mock.patch.object(agent_module, "_transport_backoff"):
            with self.assertRaises(urllib.error.URLError):
                agent_module._ollama_chat(self.runtime, self.messages)
        self.assertEqual(posts.call_count, 3)

    def test_exhausted_retries_keep_guard_answer_in_turn(self):
        """3 consecutive transport failures at the HTTP layer -> the existing
        PT-BR guard answer, texts unchanged (no create/serve ran -> no
        synthesis)."""
        with unittest.mock.patch.object(
            agent_module,
            "_transport_post",
            side_effect=[urllib.error.URLError("down")] * 3,
        ), unittest.mock.patch.object(agent_module, "_transport_backoff"):
            response = run_turn(self.runtime, None, "status?", llm_fn=None)
        self.assertEqual(response["turn"], [])
        self.assertIn("não consegui falar com o modelo de IA", response["answer"])
        self.assertIn("Ollama", response["answer"])
        self.assertNotIn("phase", response)


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

    def test_raw_bookkeeping_rows_are_skipped_from_context(self):
        records = [
            {"role": "user", "content": "ping"},
            {"role": "assistant_raw", "phase": "attempt", "raw_model_output": "drift prose x" * 50},
            {"role": "assistant", "answer": "olá", "urls": []},
        ]
        rendered = build_context("SYS", records)
        self.assertEqual([m["role"] for m in rendered], ["system", "user", "assistant"])
        self.assertNotIn("drift prose", json.dumps(rendered, ensure_ascii=False))

    def test_large_tool_args_redacted_small_args_verbatim(self):
        records = [
            {"role": "user", "content": "dashboard"},
            {
                "role": "assistant",
                "tool": "dashboard.create",
                "args": {"title": "T", "html": LARGE_HTML, "data_source": "app.data:finance_summary"},
                "result": {"artifact_id": "ab" * 16, "path": "/artifacts/" + "ab" * 16 + ".html"},
                "ts": "t",
            },
            {"role": "assistant", "tool": "app.data", "args": {"dataset": "health"}, "result": {"ok": True}, "ts": "t"},
        ]
        rendered = build_context("SYS", records)
        create_message = rendered[-2]["content"]
        self.assertNotIn("AAAA", create_message)  # marker-agnostic size guard
        self.assertNotIn(DRIFT_HTML_SENTINEL, create_message)
        self.assertIn('"args_summary"', create_message)
        self.assertIn("stored in the session file, not re-sent", create_message)
        self.assertIn('"artifact_hint": "abababab\u2026html"', create_message)
        # The result row stays intact (small): full artifact id visible.
        self.assertIn('"artifact_id": "' + "ab" * 16, create_message)
        # Small args of other tools render verbatim (unchanged behavior).
        self.assertIn('"dataset": "health"', rendered[-1]["content"])

    def test_summarize_tool_args_passthrough_and_marker(self):
        self.assertEqual(summarize_tool_args(None), "")
        self.assertEqual(summarize_tool_args({}), "")
        self.assertEqual(summarize_tool_args({"dataset": "health"}), "")  # nothing redacted
        summary = json.loads(summarize_tool_args({"html": LARGE_HTML, "title": "T"}))
        self.assertEqual(summary["title"], "T")
        self.assertIn("stored in the session file, not re-sent", summary["html"])
        self.assertNotIn(DRIFT_HTML_SENTINEL, summary["html"])

    def test_truncate_raw_model_output_caps_8k(self):
        huge = "D" * (RAW_MODEL_OUTPUT_MAX_BYTES + 500)
        trimmed = truncate_raw_model_output(huge)
        self.assertLessEqual(len(trimmed.encode("utf-8")), RAW_MODEL_OUTPUT_MAX_BYTES)
        self.assertIn("raw model output truncated", trimmed)
        self.assertIn("kept", trimmed)
        # Small outputs (and non-str defensively) pass through / degrade safely.
        self.assertEqual(truncate_raw_model_output("hi"), "hi")
        self.assertEqual(truncate_raw_model_output(None), "")

    def test_continuation_reminder_states(self):
        success = {
            "role": "assistant",
            "tool": "dashboard.create",
            "args": {"title": "T", "html": "<!DOCTYPE html><p>x</p>"},
            "result": {"artifact_id": "cd" * 16, "path": "/x"},
        }
        reminder = continuation_reminder_for([{"role": "user", "content": "go"}, success])
        self.assertIn('"tool": "server.serve", "args": {"action": "ensure", "artifact_id": "' + "cd" * 16 + '"}', reminder)
        self.assertIn('"answer"', reminder)  # the answer shape follows the tool template
        # Defensive fallback: a create-success at the newest meaningful slot
        # (e.g. after a cross-continuation turn) still gets the serve recipe.
        self.assertIn("server.serve", reminder)
        # Auto_serve (loop-executed) -> the FINALIZE recipe with the served URL.
        auto_serve = {
            "role": "assistant",
            "tool": "server.serve",
            "args": {"action": "ensure", "artifact_id": "cd" * 16},
            "phase": AUTO_SERVE_PHASE,
            "result": {"url": "http://127.0.0.1:4770/" + "cd" * 16 + ".html"},
        }
        finalize = continuation_reminder_for([{"role": "user", "content": "go"}, auto_serve])
        self.assertIn("urls\": [\"http://127.0.0.1:4770/" + "cd" * 16 + ".html\"]", finalize)
        self.assertIn("Do NOT call any tool again", finalize)
        self.assertNotIn('"args": {"action": "ensure"', finalize)  # no tool-call template
        # Any other newest meaningful record -> None (generic paths untouched).
        self.assertIsNone(continuation_reminder_for([]))
        self.assertIsNone(continuation_reminder_for([{"role": "user", "content": "go"}]))
        self.assertIsNone(continuation_reminder_for([success, {"role": "assistant", "answer": "done", "urls": []}]))
        # A MODEL-driven serve step (no auto_serve phase) gets no finalize.
        self.assertIsNone(
            continuation_reminder_for(
                [success, {"role": "assistant", "tool": "server.serve", "args": {}, "result": {"url": "u"}}]
            )
        )
        self.assertIsNone(continuation_reminder_for([{**success, "error": "boom", "result": None}]))
        # Errored / url-less auto_serve step -> None (generic reminder path).
        self.assertIsNone(
            continuation_reminder_for([{**auto_serve, "error": "artifact not found: x", "result": None}])
        )
        self.assertIsNone(continuation_reminder_for([{**auto_serve, "result": {}}]))
        # Raw bookkeeping rows never mask the underlying create step.
        self.assertIsNotNone(
            continuation_reminder_for(
                [success, {"role": "assistant_raw", "phase": "attempt", "raw_model_output": "drift"}]
            )
        )

    def test_truncation_caps_at_4k_with_marker(self):
        payload = json.dumps({"items": ["x" * 100] * 100})
        trimmed = truncate_tool_result_text(payload)
        self.assertLessEqual(len(trimmed.encode("utf-8")), 4096)
        self.assertIn("truncated", trimmed)
        self.assertIn("persisted in the session file", trimmed)
        # untouched when small
        self.assertEqual(truncate_tool_result_text('{"ok": true}'), '{"ok": true}')


# ---- round 3: session-context hardening ------------------------------------------


class TestSessionContextHardening(unittest.TestCase):
    """Round 3 hardening (session f1b57e96 evidence: the prior turn's long
    assistant answer echoed into the context made the NEXT fresh ask drift out
    of protocol on its first model turn).

    (a) prior answers longer than ~240 chars truncate in the model CONTEXT
        only (first ~200 chars + ``…[answer truncated N chars in context]``) —
        the full text stays in the session jsonl on disk and the UI;
    (b) turns on a session with prior history carry ONE standing protocol
        reminder line in the system prompt (every prompt of the turn);
    (c) the happy path (full dashboard turn + a second simple turn on the
        same session) still completes normally;
    (d) the double-drift guard is unchanged on continuing sessions.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.runtime = make_runtime(self.tmp.name)
        self.addCleanup(self.runtime.agent_registry.stop)

    # -- (a) prior-answer truncation ------------------------------------------------

    def test_truncate_prior_answer_text_constants_boundary_and_marker(self):
        short = "Dashboard pronto: http://127.0.0.1:4770/x.html."
        self.assertEqual(truncate_prior_answer_text(short), short)
        boundary = "b" * PRIOR_ANSWER_INLINE_MAX_CHARS  # exactly ~240: untouched
        self.assertEqual(truncate_prior_answer_text(boundary), boundary)
        over = "x" * (PRIOR_ANSWER_INLINE_MAX_CHARS + 1)  # 241: truncated
        trimmed = truncate_prior_answer_text(over)
        self.assertEqual(trimmed[:PRIOR_ANSWER_KEPT_CHARS], "x" * PRIOR_ANSWER_KEPT_CHARS)
        self.assertIn("…[answer truncated 41 chars in context]", trimmed)
        # Defensive non-str degradation (the loop only ever writes str answers).
        self.assertEqual(truncate_prior_answer_text(None), "")
        self.assertEqual(truncate_prior_answer_text(42), "42")

    def test_build_context_truncates_long_answers_short_verbatim(self):
        short_answer = "Resposta curta e ok."
        long_answer = "HEAD-PROSE " + "q" * PRIOR_ANSWER_KEPT_CHARS + " TAIL-" + DRIFT_HTML_SENTINEL * 2
        omitted = len(long_answer) - PRIOR_ANSWER_KEPT_CHARS
        records = [
            {"role": "user", "content": "um"},
            {"role": "assistant", "answer": short_answer, "urls": []},
            {"role": "user", "content": "dois"},
            {"role": "assistant", "answer": long_answer, "urls": []},
            {"role": "user", "content": "três"},
        ]
        dump = json.dumps(build_context("SYS", records), ensure_ascii=False)
        self.assertIn(short_answer, dump)  # short answers stay verbatim
        self.assertNotIn(long_answer, dump)  # the long full text is NOT re-sent
        self.assertNotIn(DRIFT_HTML_SENTINEL, dump)  # no tail fragment either
        self.assertLessEqual(len(long_answer[:PRIOR_ANSWER_KEPT_CHARS]), 200)
        self.assertIn(long_answer[:PRIOR_ANSWER_KEPT_CHARS], dump)  # head survives
        self.assertIn(f"…[answer truncated {omitted} chars in context]", dump)

    def test_end_to_end_long_prior_answers_truncated_full_on_disk(self):
        """(a) end-to-end: BOTH guard-style and successful long prior answers
        render truncated in the very next outgoing prompt; the session jsonl
        keeps every full answer."""
        store = AgentSessionStore(self.runtime.config_dir)
        session_id = store.new_session_id()
        success_long = "S" * 260  # previous turn's successful answer
        guard_long = "G" * 280  # a long guard-error-style answer
        store.append(session_id, {"role": "user", "content": "primeiro pedido"})
        store.append(session_id, {"role": "assistant", "answer": success_long, "urls": []})
        store.append(session_id, {"role": "user", "content": "segundo pedido"})
        store.append(session_id, {"role": "assistant", "answer": guard_long, "urls": []})

        llm = ScriptedLLM([json.dumps({"answer": "terceira resposta", "urls": []})])
        response = run_turn(self.runtime, session_id, "terceira pergunta", llm_fn=llm)
        self.assertEqual(llm.call_count, 1)
        self.assertEqual(response["answer"], "terceira resposta")

        outgoing = json.dumps([m["content"] for m in llm.calls[0]], ensure_ascii=False)
        self.assertIn("…[answer truncated 60 chars in context]", outgoing)
        self.assertIn("…[answer truncated 80 chars in context]", outgoing)
        self.assertIn("S" * PRIOR_ANSWER_KEPT_CHARS, outgoing)  # heads ARE re-sent
        self.assertIn("G" * PRIOR_ANSWER_KEPT_CHARS, outgoing)
        self.assertNotIn(success_long, outgoing)  # full texts never re-sent
        self.assertNotIn(guard_long, outgoing)

        # Disk keeps the FULL answers (context-only truncation; the UI reads
        # the same records verbatim).
        records = store.load(session_id)
        self.assertEqual(records[1]["answer"], success_long)
        self.assertEqual(records[3]["answer"], guard_long)

    # -- (b) standing protocol reminder ---------------------------------------------

    def test_standing_reminder_constant_exact_english(self):
        self.assertEqual(
            STANDING_PROTOCOL_REMINDER,
            "Reminder: respond with exactly ONE JSON object \u2014 "
            '{"tool": "\u2026", "args": {...}} or {"answer": "\u2026", "urls": [...]} \u2014 '
            "nothing else.",
        )

    def test_standing_reminder_on_nonempty_session_only_and_once(self):
        # Fresh session: NO standing reminder anywhere in the outgoing prompt.
        fresh = ScriptedLLM([json.dumps({"answer": "olá, nova conversa", "urls": []})])
        fresh_response = run_turn(self.runtime, None, "primeiro contato", llm_fn=fresh)
        fresh_contents = [str(m.get("content", "")) for m in fresh.calls[0]]
        self.assertEqual(sum(c.count(STANDING_PROTOCOL_REMINDER) for c in fresh_contents), 0)

        # Continuing the fresh session: reminder present EXACTLY ONCE, as the
        # last line of the system message (grounded header + template + line).
        llm = ScriptedLLM([json.dumps({"answer": "de novo, em protocolo", "urls": []})])
        response = run_turn(self.runtime, fresh_response["session_id"], "segunda msg", llm_fn=llm)
        self.assertEqual(llm.call_count, 1)
        self.assertEqual(response["answer"], "de novo, em protocolo")
        contents = [str(m.get("content", "")) for m in llm.calls[0]]
        self.assertEqual(sum(c.count(STANDING_PROTOCOL_REMINDER) for c in contents), 1)
        system_message = llm.calls[0][0]
        self.assertEqual(system_message["role"], "system")
        self.assertTrue(system_message["content"].endswith(STANDING_PROTOCOL_REMINDER))
        # The grounded date header and template stay intact above the line.
        self.assertIn("Current local date: ", system_message["content"])
        prompt_without_reminder = system_message["content"][: -len(STANDING_PROTOCOL_REMINDER)]
        self.assertIn(load_system_prompt(), prompt_without_reminder)

    def test_standing_reminder_carried_on_protocol_retry_prompt(self):
        store = AgentSessionStore(self.runtime.config_dir)
        session_id = store.new_session_id()
        store.append(session_id, {"role": "user", "content": "primeiro pedido"})
        store.append(session_id, {"role": "assistant", "answer": "curta anterior", "urls": []})

        llm = ScriptedLLM(
            ["prosa livre sem JSON", json.dumps({"answer": "agora em protocolo", "urls": []})]
        )
        response = run_turn(self.runtime, session_id, "tenta de novo", llm_fn=llm)
        self.assertEqual(llm.call_count, 2)
        self.assertEqual(response["answer"], "agora em protocolo")
        # EVERY prompt of the turn (initial + protocol retry) carries exactly
        # one standing reminder; the user-role retry tail stays untouched.
        for messages in llm.calls:
            contents = [str(m.get("content", "")) for m in messages]
            self.assertEqual(sum(c.count(STANDING_PROTOCOL_REMINDER) for c in contents), 1)
            self.assertEqual(contents[-1].count(STANDING_PROTOCOL_REMINDER), 0)

    # -- (c) happy-path regression ---------------------------------------------------

    def test_full_dashboard_turn_then_simple_turn_regression(self):
        """(c) session-2-style conversation with the scripted LLM: the full
        dashboard pipeline completes, and the NEXT fresh ask on the same
        session completes in protocol too."""
        llm = ScriptedLLM(
            [
                json.dumps({"tool": "app.data", "args": {"dataset": "health"}}),
                json.dumps(
                    {
                        "tool": "dashboard.create",
                        "args": {"title": "Health Dashboard", "html": DASHBOARD_HTML, "data_source": "app.data:health"},
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
        self.assertEqual(llm.call_count, 3)
        self.assertEqual([step["tool"] for step in response["turn"]],
                         ["app.data", "dashboard.create", "server.serve"])
        self.assertEqual(response["turn"][2].get("phase"), AUTO_SERVE_PHASE)
        self.assertEqual(len(response["urls"]), 1)
        with urllib.request.urlopen(response["urls"][0], timeout=5) as resp:
            self.assertEqual(resp.status, 200)

        # Second SIMPLE turn on the same session (the session-2 "fresh ask").
        llm2 = ScriptedLLM([json.dumps({"answer": "Enviei as notificações de teste.", "urls": []})])
        second = run_turn(self.runtime, response["session_id"], "envie notificações de teste", llm_fn=llm2)
        self.assertEqual(llm2.call_count, 1)
        self.assertEqual(second["answer"], "Enviei as notificações de teste.")
        self.assertEqual(second["turn"], [])
        self.assertEqual(second["session_id"], response["session_id"])
        system_content = llm2.calls[0][0]["content"]
        self.assertEqual(system_content.count(STANDING_PROTOCOL_REMINDER), 1)
        self.assertTrue(system_content.endswith(STANDING_PROTOCOL_REMINDER))
        # The prior dashboard answer is short: verbatim in context, and NO
        # truncation marker pollution on the happy path.
        outgoing = json.dumps([m["content"] for m in llm2.calls[0]], ensure_ascii=False)
        self.assertNotIn("answer truncated", outgoing)
        self.assertIn("Aqui está o dashboard do bot", outgoing)
        # Both turns persisted in the SAME session jsonl.
        records = AgentSessionStore(self.runtime.config_dir).load(second["session_id"])
        self.assertEqual(records[0]["content"], "me mostre um dashboard, por favor")
        self.assertEqual(records[-1]["answer"], second["answer"])

    # -- (d) guard behavior unchanged --------------------------------------------------

    def test_double_drift_guard_unchanged_on_continuing_session(self):
        store = AgentSessionStore(self.runtime.config_dir)
        session_id = store.new_session_id()
        store.append(session_id, {"role": "user", "content": "primeiro pedido"})
        store.append(session_id, {"role": "assistant", "answer": "resposta anterior", "urls": []})

        llm = ScriptedLLM(["prosa numero um", "prosa numero dois"])
        response = run_turn(self.runtime, session_id, "tenta mais uma vez", llm_fn=llm)
        self.assertEqual(llm.call_count, 2)
        self.assertIn("formato JSON", response["answer"])
        self.assertIn("duas vezes", response["answer"])
        self.assertIn("tente reformular o pedido", response["answer"])
        self.assertEqual(response["urls"], [])
        self.assertNotIn("phase", response)
        # Session preserved with post-mortem raw rows; the retry prompt kept
        # the standing reminder (non-empty history) without masking the guard.
        records = store.load(session_id)
        self.assertEqual([r.get("role") for r in records],
                         ["user", "assistant", "user", "assistant_raw", "assistant_raw", "assistant"])
        self.assertEqual([r["phase"] for r in records if r.get("role") == "assistant_raw"],
                         ["attempt", "protocol_retry"])
        self.assertEqual(records[-1]["answer"], response["answer"])
        retry_contents = [str(m.get("content", "")) for m in llm.calls[1]]
        self.assertEqual(sum(c.count(STANDING_PROTOCOL_REMINDER) for c in retry_contents), 1)


# ---- auto serve + deterministic completion ----------------------------------------


class TestAutoServeLoop(unittest.TestCase):
    """Loop-enforced recipe completion: server.serve runs WITHOUT a model
    call after a successful model-driven dashboard.create (does not consume a
    MAX_TOOL_TURNS slot), and a completed pipeline synthesizes the answer."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.runtime = make_runtime(self.tmp.name)
        self.addCleanup(self.runtime.agent_registry.stop)

    def test_cap_counts_model_turns_only_and_completes_synthesized(self):
        """6 model tool turns (5x app.data + create) exhaust the cap; the
        loop STILL appends the auto_serve serve step as a 7th step and the
        turn finally completes with the synthesized answer."""
        llm = ScriptedLLM(
            [json.dumps({"tool": "app.data", "args": {"dataset": "health"}}) for _ in range(5)]
            + [
                json.dumps(
                    {
                        "tool": "dashboard.create",
                        "args": {"title": "Health Dashboard", "html": DASHBOARD_HTML, "data_source": "app.data:health"},
                    }
                ),
            ]
        )
        response = run_turn(self.runtime, None, "dashboard completo", llm_fn=llm)
        self.assertEqual(llm.call_count, 6)  # model turns only; serve was loop-driven
        self.assertEqual(len(response["turn"]), 7)  # 5x app.data + create + auto_serve
        self.assertEqual([step["tool"] for step in response["turn"]][5], "dashboard.create")
        self.assertEqual(response["turn"][6].get("phase"), AUTO_SERVE_PHASE)
        self.assertIn("Dashboard pronto:", response["answer"])
        match = URL_RE.match(response["urls"][0])
        self.assertIsNotNone(match, response["urls"])
        self.assertEqual(response["phase"], AUTO_COMPLETE_PHASE)
        with urllib.request.urlopen(response["urls"][0], timeout=5) as resp:
            self.assertEqual(resp.status, 200)
        records = AgentSessionStore(self.runtime.config_dir).load(response["session_id"])
        self.assertEqual(len(records), 15)  # user + 6x(raw, step) + auto_serve + answer

    def test_llm_failure_after_completed_pipeline_synthesizes(self):
        """Transport death right after create->serve: the user still gets the
        dashboard (synthesized), not the 'não consegui falar' guard."""
        create_json = json.dumps(
            {
                "tool": "dashboard.create",
                "args": {"title": "Health Dashboard", "html": DASHBOARD_HTML, "data_source": "app.data:health"},
            }
        )

        def flaky_llm(messages):
            flaky_llm.calls.append(messages)
            if len(flaky_llm.calls) == 1:
                return create_json
            raise TimeoutError("transport died right after auto-serve")

        flaky_llm.calls = []
        response = run_turn(self.runtime, None, "me mostre um dashboard", llm_fn=flaky_llm)
        self.assertEqual(len(flaky_llm.calls), 2)
        self.assertEqual([step["tool"] for step in response["turn"]], ["dashboard.create", "server.serve"])
        self.assertEqual(response["turn"][1].get("phase"), AUTO_SERVE_PHASE)
        self.assertIn("Dashboard pronto:", response["answer"])
        self.assertEqual(response["phase"], AUTO_COMPLETE_PHASE)
        with urllib.request.urlopen(response["urls"][0], timeout=5) as resp:
            self.assertEqual(resp.status, 200)
        records = AgentSessionStore(self.runtime.config_dir).load(response["session_id"])
        self.assertEqual(records[-1]["phase"], AUTO_COMPLETE_PHASE)


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
        self.assertEqual(roles, ["user", "assistant_raw", "assistant"])
        self.assertEqual(detail["records"][0]["content"], "Olá bot! Tudo bem?")
        self.assertEqual(detail["records"][-1]["answer"], body["answer"])

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