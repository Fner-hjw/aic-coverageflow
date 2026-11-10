"""Tests for the agent layer: backends (base, cli, api, replay).

Owner: stream A3. All tests run offline - no network, no real CLI logins.
Fake CLIs live under tests/fixtures/fakeclis/ and are placed on PATH.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from coverageflow.backends import base as bbase
from coverageflow.backends import cli_backends as cli
from tests.conftest import FAKECLIS, make_call


CANNED = '{"schematic": "good", "trace": "demonstration"}'


# --------------------------------------------------------------------------
# base.py - AgentResult / HealthReport / CostEstimate / Backend protocol
# --------------------------------------------------------------------------

class TestBaseDataclasses:
    def test_agent_result_defaults_match_contract(self):
        r = bbase.AgentResult(ok=True, backend="claude")
        assert r.output_text is None
        assert r.parsed is None
        assert r.error is None
        assert r.elapsed_seconds == 0.0
        assert r.tokens_in is None
        assert r.tokens_out is None
        assert r.raw_response_ref is None

    def test_health_report_fields(self):
        h = bbase.HealthReport(backend="replay", ok=True, detail="42 recordings")
        assert h.backend == "replay"
        assert h.ok is True
        assert h.checked_at  # ISO-8601 timestamp always recorded

    def test_cost_estimate_fields(self):
        c = bbase.CostEstimate(backend="api", estimated_tokens=1234, method="chars/4")
        assert c.estimated_tokens == 1234
        assert c.method == "chars/4"
        assert c.note is None

    def test_backend_protocol_runtime_checkable(self):
        class Stub:
            name = "stub"

            def healthcheck(self):
                return bbase.HealthReport(backend="stub", ok=True)

            def invoke(self, call, assembled_prompt):
                return bbase.AgentResult(ok=True, backend="stub")

            def estimate_cost(self, text):
                return bbase.CostEstimate(
                    backend="stub", estimated_tokens=1, method="chars/4"
                )

        assert isinstance(Stub(), bbase.Backend)

    def test_estimate_tokens_chars_per_four(self):
        assert bbase.estimate_tokens_chars_per_four("x" * 400) == 100
        # never zero for non-empty text
        assert bbase.estimate_tokens_chars_per_four("ab") >= 1
        assert bbase.estimate_tokens_chars_per_four("") == 0


# --------------------------------------------------------------------------
# cli_backends.py - claude / codex / kimi headless adapters
# --------------------------------------------------------------------------

BACKEND_FACTORIES = [
    ("claude", cli.ClaudeBackend),
    ("codex", cli.CodexBackend),
    ("kimi", cli.KimiBackend),
    ("vibe", cli.VibeBackend),
]
BACKEND_IDS = [name for name, _ in BACKEND_FACTORIES]


def make_backend(name):
    return dict(BACKEND_FACTORIES)[name]()


class TestCliHappyPaths:
    @pytest.mark.parametrize("name", BACKEND_IDS)
    def test_ok_output_normalized(self, name, monkeypatch):
        monkeypatch.setenv("FAKE_RESULT_TEXT", CANNED)
        backend = make_backend(name)
        res = backend.invoke(make_call(role="ingest"), "assemble me a report request")
        assert res.ok, res.error
        assert res.output_text == CANNED
        assert res.backend == name
        assert res.error is None
        assert res.elapsed_seconds >= 0
        assert res.tokens_in is not None and res.tokens_out is not None

    @pytest.mark.parametrize("name", BACKEND_IDS)
    def test_prompt_passed_on_fresh_argv(self, name, tmp_path, monkeypatch):
        log = tmp_path / "argv.jsonl"
        monkeypatch.setenv("FAKE_ARGV_LOG", str(log))
        copy = tmp_path / "prompt.txt"
        monkeypatch.setenv("FAKE_PROMPT_COPY", str(copy))
        backend = make_backend(name)
        res = backend.invoke(make_call(role="extractor"), "PROMPT-MARKER-12345")
        assert res.ok, res.error
        lines = log.read_text().strip().splitlines()
        argvs = [json.loads(x) for x in lines if x.strip()]
        assert argvs, "CLI was never invoked"
        if name == "vibe":
            # the prompt travels in a file, not argv: role prompts embed
            # whole statement slices and must not hit ARG_MAX
            assert copy.read_text() == "PROMPT-MARKER-12345"
            assert any("--prompt-file" in a for a in argvs)
        else:
            assert any("PROMPT-MARKER-12345" in a for a in argvs)

    def test_claude_uses_contract_flags(self, tmp_path, monkeypatch):
        log = tmp_path / "argv.jsonl"
        monkeypatch.setenv("FAKE_ARGV_LOG", str(log))
        cli.ClaudeBackend().invoke(make_call(), "hello")
        argv = json.loads(log.read_text().strip().splitlines()[-1])
        assert str(argv[0]).endswith("claude")
        assert "-p" in argv
        assert "--output-format" in argv
        assert "json" in argv

    def test_codex_uses_contract_flags(self, tmp_path, monkeypatch):
        log = tmp_path / "argv.jsonl"
        monkeypatch.setenv("FAKE_ARGV_LOG", str(log))
        cli.CodexBackend().invoke(make_call(), "hello")
        argv = json.loads(log.read_text().strip().splitlines()[-1])
        assert str(argv[0]).endswith("codex")
        assert "exec" in argv
        assert "--json" in argv

    def test_vibe_uses_contract_flags(self, tmp_path, monkeypatch):
        log = tmp_path / "argv.jsonl"
        monkeypatch.setenv("FAKE_ARGV_LOG", str(log))
        cli.VibeBackend().invoke(make_call(), "hello")
        argv = json.loads(log.read_text().strip().splitlines()[-1])
        assert str(argv[0]).endswith("vibe")
        assert "--prompt-file" in argv
        assert "--output" in argv and "text" in argv
        assert "--max-price" in argv  # per-call cost cap always applied
        assert "--time-limit" in argv  # CLI stops itself before the harness kill

    def test_vibe_passes_case_workdir(self, tmp_path, monkeypatch):
        log = tmp_path / "argv.jsonl"
        monkeypatch.setenv("FAKE_ARGV_LOG", str(log))
        call = make_call(role="extractor")
        call = type(call)(**{**call.__dict__, "workdir": str(tmp_path)})
        cli.VibeBackend().invoke(call, "hello")
        argv = json.loads(log.read_text().strip().splitlines()[-1])
        assert "--workdir" in argv
        assert argv[argv.index("--workdir") + 1] == str(tmp_path)

    def test_claude_real_usage_surfaced(self, monkeypatch):
        monkeypatch.setenv("FAKE_RESULT_TEXT", "x" * 40)
        res = cli.ClaudeBackend().invoke(make_call(), "y" * 400)
        assert res.tokens_in == 100  # the fake reports len(prompt)//4

    def test_codex_real_usage_surfaced(self):
        res = cli.CodexBackend().invoke(make_call(), "hello")
        assert res.tokens_in == 11 and res.tokens_out == 7

    def test_tokens_estimated_chars_per_four_when_unreported(self):
        # kimi (plain-text protocol) reports no usage on stdout.
        monkey_env_text = "z" * 800
        res = cli.KimiBackend().invoke(make_call(), monkey_env_text)
        assert res.tokens_in == 200  # chars/4 fallback
        assert res.tokens_out == len(CANNED) // 4

    @pytest.mark.parametrize("name", BACKEND_IDS)
    def test_estimate_cost(self, name):
        est = make_backend(name).estimate_cost("A" * 400)
        assert est is not None
        assert est.estimated_tokens == 100
        assert est.method == "chars/4"

    @pytest.mark.parametrize("name", BACKEND_IDS)
    def test_healthcheck_ok_when_binary_present(self, name):
        report = make_backend(name).healthcheck()
        assert report.ok, report.detail
        assert report.backend == name
        assert report.checked_at


class TestCliFailureModes:
    """Every adapter converts every failure into AgentResult(ok=False) - never a crash."""

    @pytest.mark.parametrize("name", BACKEND_IDS)
    def test_nonzero_exit_is_error(self, name, monkeypatch):
        monkeypatch.setenv("FAKE_BEHAVIOR", "fail")
        res = make_backend(name).invoke(make_call(), "hello")
        assert res.ok is False
        assert res.error and "exit" in res.error.lower()
        # stderr captured into the error message
        assert res.error and ("login" in res.error or "auth" in res.error or "authentic" in res.error)

    @pytest.mark.parametrize("name", BACKEND_IDS)
    def test_timeout_kills_process(self, name, monkeypatch):
        monkeypatch.setenv("FAKE_BEHAVIOR", "sleep")
        monkeypatch.setenv("FAKE_SLEEP_SECONDS", "30")
        res = make_backend(name).invoke(make_call(timeout_seconds=1.0), "hello")
        assert res.ok is False
        assert res.error and "timeout" in res.error.lower()
        assert res.elapsed_seconds < 10  # returned fast; process killed

    @pytest.mark.parametrize("name", BACKEND_IDS)
    def test_missing_binary_is_error(self, name):
        cls = dict(BACKEND_FACTORIES)[name]
        backend = cls(cli_path="/nonexistent/binary/that/is/missing")
        res = backend.invoke(make_call(), "hello")
        assert res.ok is False
        assert res.error and "not found" in res.error.lower()
        assert backend.healthcheck().ok is False

    @pytest.mark.parametrize("name", BACKEND_IDS)
    def test_stderr_garbage_with_zero_exit_still_ok(self, name, monkeypatch):
        monkeypatch.setenv("FAKE_BEHAVIOR", "stderr")
        res = make_backend(name).invoke(make_call(), "hello")
        assert res.ok is True, res.error

    @pytest.mark.parametrize("name,expect_json", [("claude", True), ("codex", True), ("kimi", False), ("vibe", False)])
    def test_nonjson_stdout(self, name, expect_json, monkeypatch):
        # claude/codex promised JSON envelopes: garbage stdout must fail.
        # kimi's protocol is plain stdout text: raw text passes through
        # unchanged (JSON-type validation is dispatch's job).
        monkeypatch.setenv("FAKE_BEHAVIOR", "garbage")
        res = make_backend(name).invoke(make_call(), "hello")
        if expect_json:
            assert res.ok is False
            assert res.error and "parse" in res.error.lower()
        else:
            assert res.ok is True
            assert res.output_text == "pong! (not json)"


class TestCliTimeoutAndPartialOutput:
    """A call that runs out of time must not lose what the CLI already wrote."""

    def test_partial_output_preserved_on_timeout(self, monkeypatch):
        # the capture logic lives in the shared base runner; the vibe fake is
        # the one that can emit-then-hang
        monkeypatch.setenv("FAKE_BEHAVIOR", "partial")
        monkeypatch.setenv("FAKE_SLEEP_SECONDS", "30")
        res = cli.VibeBackend().invoke(make_call(timeout_seconds=1.0), "hello")
        assert res.ok is False
        assert "timeout" in res.error.lower()
        assert "partial output" in res.error.lower()
        assert res.output_text and '{"role": "extractor"' in res.output_text
        assert res.tokens_out is not None and res.tokens_out > 0

    def test_soft_exit_code_with_output_is_accepted(self, monkeypatch):
        # the CLI's own --time-limit stop writes its export and exits 3
        monkeypatch.setenv("FAKE_BEHAVIOR", "soft_stop")
        monkeypatch.setenv("FAKE_RESULT_TEXT", '{"stopped": "early"}')
        res = cli.VibeBackend().invoke(make_call(), "hello")
        assert res.ok is True, res.error
        assert res.output_text == '{"stopped": "early"}'
        assert "stopped early" in (res.error or "")

    def test_soft_exit_code_without_output_is_an_error(self, monkeypatch):
        monkeypatch.setenv("FAKE_BEHAVIOR", "soft_stop")
        monkeypatch.setenv("FAKE_RESULT_TEXT", "")
        res = cli.VibeBackend().invoke(make_call(), "hello")
        assert res.ok is False

    def test_utf8_output_survives_a_non_utf8_locale(self, monkeypatch):
        monkeypatch.setenv("LC_ALL", "en_US.ISO8859-1")
        monkeypatch.setenv("LANG", "en_US.ISO8859-1")
        monkeypatch.setenv("FAKE_RESULT_TEXT", "营收 13,500 — 同比增长")
        res = cli.VibeBackend().invoke(make_call(), "hello")
        assert res.ok, res.error
        assert res.output_text == "营收 13,500 — 同比增长"

    def test_timeout_kills_the_whole_process_group(self, tmp_path, monkeypatch):
        # the fake spawns a grandchild; a timeout must not orphan it
        pid_file = tmp_path / "child.pid"
        monkeypatch.setenv("FAKE_BEHAVIOR", "partial")
        monkeypatch.setenv("FAKE_SLEEP_SECONDS", "30")
        monkeypatch.setenv("FAKE_CHILD_PID", str(pid_file))
        res = cli.VibeBackend().invoke(make_call(timeout_seconds=1.0), "hello")
        assert res.ok is False
        assert pid_file.exists(), "fake never spawned its grandchild"
        pid = int(pid_file.read_text())
        deadline = time.time() + 5
        while time.time() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return  # grandchild is gone: the group kill worked
            time.sleep(0.1)
        pytest.fail("grandchild survived the timeout kill (orphaned process)")


class TestClaudeExtras:
    def test_is_error_envelope_fails(self, monkeypatch):
        monkeypatch.setenv("FAKE_BEHAVIOR", "error_result")
        res = cli.ClaudeBackend().invoke(make_call(), "hello")
        assert res.ok is False


class TestKimiDetection:
    def test_detects_headless_flag_from_help(self, monkeypatch):
        backend = cli.KimiBackend()
        flags = backend._detect_headless_flags()
        assert "-p" in flags

    def test_degrades_when_no_headless_support(self, monkeypatch):
        monkeypatch.setenv("FAKE_KIMI_MODE", "limited")
        backend = cli.KimiBackend()
        report = backend.healthcheck()
        assert report.ok is False
        assert "headless" in report.detail.lower()
        res = backend.invoke(make_call(), "hello")
        assert res.ok is False
        assert res.error and "headless" in res.error.lower()

    def test_help_detection_reruns_after_env_change(self, monkeypatch):
        monkeypatch.setenv("FAKE_KIMI_MODE", "limited")
        backend = cli.KimiBackend()
        assert backend.healthcheck().ok is False
        monkeypatch.setenv("FAKE_KIMI_MODE", "headless")
        backend.invalidate_detection()  # explicit re-detect (e.g. CLI upgraded)
        assert backend.healthcheck().ok is True


# --------------------------------------------------------------------------
# api_backend.py - OpenAI-compatible chat completions
# --------------------------------------------------------------------------

import httpx


def _stub_handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": "chatcmpl-1",
            "choices": [
                {"index": 0, "message": {"role": "assistant", "content": CANNED}}
            ],
            "usage": {"prompt_tokens": 101, "completion_tokens": 9},
        },
    )


class TestApiBackend:
    def _backend(self, transport=None, sleeps=None):
        from coverageflow.backends import api_backend as api

        kwargs = {}
        if transport is not None:
            kwargs["transport"] = transport
        if sleeps is not None:
            kwargs["sleep"] = sleeps.append
        return api.ApiBackend(
            base_url="https://stub.local/v1",
            model="stub-model",
            api_key="sk-test",
            **kwargs,
        )

    def test_ok(self):
        backend = self._backend(transport=httpx.MockTransport(_stub_handler))
        res = backend.invoke(make_call(), "hello prompt")
        assert res.ok, res.error
        assert res.output_text == CANNED
        assert res.tokens_in == 101 and res.tokens_out == 9
        assert res.backend == "api"
        assert res.elapsed_seconds >= 0

    def test_request_shape(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            seen["json"] = json.loads(request.content)
            seen["auth"] = request.headers.get("Authorization")
            return _stub_handler(request)

        backend = self._backend(transport=httpx.MockTransport(handler))
        res = backend.invoke(make_call(timeout_seconds=30), "PAYLOAD-PROMPT")
        assert res.ok, res.error
        assert seen["url"] == "https://stub.local/v1/chat/completions"
        assert seen["auth"] == "Bearer sk-test"
        assert seen["json"]["model"] == "stub-model"
        assert seen["json"]["messages"][0]["content"] == "PAYLOAD-PROMPT"

    def test_retries_5xx_then_succeeds(self):
        attempts = []

        def handler(request: httpx.Request) -> httpx.Response:
            attempts.append(1)
            if len(attempts) < 3:
                return httpx.Response(500, text="server exploded")
            return _stub_handler(request)

        sleeps = []
        backend = self._backend(
            transport=httpx.MockTransport(handler), sleeps=sleeps
        )
        res = backend.invoke(make_call(), "hello")
        assert res.ok, res.error
        assert len(attempts) == 3
        assert sleeps == [0.5, 1.0]  # exponential backoff between attempts

    def test_retries_timeout_then_gives_up(self):
        attempts = []
        sleeps = []

        def handler(request: httpx.Request) -> httpx.Response:
            attempts.append(1)
            raise httpx.ConnectTimeout("timed out")

        backend = self._backend(
            transport=httpx.MockTransport(handler), sleeps=sleeps
        )
        res = backend.invoke(make_call(), "hello")
        assert res.ok is False
        assert len(attempts) == 3
        assert res.error and "timeout" in res.error.lower()

    def test_4xx_is_not_retried(self):
        attempts = []

        def handler(request: httpx.Request) -> httpx.Response:
            attempts.append(1)
            return httpx.Response(401, text="bad key")

        backend = self._backend(transport=httpx.MockTransport(handler))
        res = backend.invoke(make_call(), "hello")
        assert res.ok is False
        assert len(attempts) == 1
        assert "401" in res.error

    def test_unconfigured_backend_fails_cleanly(self, monkeypatch):
        from coverageflow.backends import api_backend as api

        for var in (
            "COVERAGEFLOW_BASE_URL",
            "COVERAGEFLOW_MODEL",
            "COVERAGEFLOW_API_KEY",
            "COVERAGEFLOW_CONFIG",
        ):
            monkeypatch.delenv(var, raising=False)
        backend = api.ApiBackend(config_path="/nonexistent/config.json")
        res = backend.invoke(make_call(), "hello")
        assert res.ok is False
        assert res.error and "not configured" in res.error
        assert backend.healthcheck().ok is False

    def test_env_configuration(self, monkeypatch):
        from coverageflow.backends import api_backend as api

        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            return _stub_handler(request)

        monkeypatch.setenv("COVERAGEFLOW_BASE_URL", "https://envstub.local/v1")
        monkeypatch.setenv("COVERAGEFLOW_MODEL", "env-model")
        monkeypatch.setenv("COVERAGEFLOW_API_KEY", "sk-env")
        backend = api.ApiBackend(transport=httpx.MockTransport(handler))
        assert backend.healthcheck().ok
        res = backend.invoke(make_call(), "x")
        assert res.ok, res.error
        assert seen["url"] == "https://envstub.local/v1/chat/completions"

    def test_config_file_fallback_and_env_precedence(self, monkeypatch, tmp_path):
        from coverageflow.backends import api_backend as api

        cfg = tmp_path / "config.json"
        cfg.write_text(
            json.dumps(
                {
                    "api": {
                        "base_url": "https://filestub.local/v1",
                        "model": "file-model",
                        "api_key": "sk-file",
                    }
                }
            )
        )
        for var in (
            "COVERAGEFLOW_BASE_URL",
            "COVERAGEFLOW_MODEL",
            "COVERAGEFLOW_API_KEY",
            "COVERAGEFLOW_CONFIG",
        ):
            monkeypatch.delenv(var, raising=False)
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            if seen.get("auth"):
                seen["url2"] = str(request.url)
            seen["auth"] = request.headers.get("Authorization")
            return _stub_handler(request)

        backend = api.ApiBackend(
            config_path=str(cfg), transport=httpx.MockTransport(handler)
        )
        assert backend.healthcheck().ok
        res = backend.invoke(make_call(), "x")
        assert res.ok, res.error
        assert seen["url"] == "https://filestub.local/v1/chat/completions"
        assert seen["auth"] == "Bearer sk-file"

        # env wins over the config file
        monkeypatch.setenv("COVERAGEFLOW_BASE_URL", "https://envstub.local/v1")
        monkeypatch.delenv("COVERAGEFLOW_API_KEY", raising=False)
        backend2 = api.ApiBackend(
            config_path=str(cfg), transport=httpx.MockTransport(handler)
        )
        res = backend2.invoke(make_call(), "x")
        assert res.ok, res.error
        assert seen["url"] == "https://envstub.local/v1/chat/completions"
        assert seen["auth"] == "Bearer sk-file"  # key still from file

    def test_malformed_success_payload_fails_cleanly(self):
        backend = self._backend(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"nope": 1}))
        )
        res = backend.invoke(make_call(), "x")
        assert res.ok is False
        assert res.error and "choices" in res.error

    def test_estimate_cost(self):
        backend = self._backend()
        est = backend.estimate_cost("B" * 400)
        assert est.estimated_tokens == 100
        assert est.method == "chars/4"

    def test_usage_absent_falls_back_to_estimated_tokens_in(self):
        """Local OpenAI-compatible servers omit `usage`; tokens_in must survive."""
        payload = {"choices": [{"message": {"content": CANNED}}]}
        backend = self._backend(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, json=payload))
        )
        res = backend.invoke(make_call(), "P" * 400)
        assert res.ok, res.error
        assert res.tokens_in == 100  # chars/4 of the prompt we actually sent
        assert res.tokens_out == bbase.estimate_tokens_chars_per_four(CANNED)

    @pytest.mark.parametrize(
        "payload",
        [
            {"choices": [{"message": None}]},
            {"choices": [None]},
            {"choices": ["not-an-object"]},
            {"choices": [{"message": "not-an-object"}]},
        ],
    )
    def test_non_object_choice_or_message_fails_cleanly(self, payload):
        backend = self._backend(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, json=payload))
        )
        res = backend.invoke(make_call(), "x")
        assert res.ok is False
        assert res.error and "content" in res.error

    def test_timeout_budget_is_per_call_not_per_attempt(self, monkeypatch):
        """Three 240s attempts must not outlive a 240s budget."""
        from coverageflow.backends import api_backend as api

        class Clock:
            now = 0.0

            def monotonic(self):
                return self.now

            def sleep(self, _seconds):
                pass

        clock = Clock()
        monkeypatch.setattr(api, "time", clock)
        attempts = []

        def handler(request: httpx.Request) -> httpx.Response:
            attempts.append(1)
            clock.now += 200.0  # each attempt burns 200s of the 240s budget
            raise httpx.ConnectTimeout("timed out")

        backend = self._backend(transport=httpx.MockTransport(handler))
        res = backend.invoke(make_call(timeout_seconds=240), "hello")
        assert res.ok is False
        assert len(attempts) == 2, "third attempt would exceed the call budget"
        assert "attempts=2" in res.error

    def test_exhausted_budget_makes_no_attempt(self):
        attempts = []

        def handler(request: httpx.Request) -> httpx.Response:
            attempts.append(1)
            return _stub_handler(request)

        backend = self._backend(transport=httpx.MockTransport(handler))
        res = backend.invoke(make_call(timeout_seconds=0), "hello")
        assert res.ok is False
        assert attempts == []
        assert "exhausted" in res.error

    def test_invalid_url_is_reported_not_raised(self):
        from coverageflow.backends import api_backend as api

        backend = api.ApiBackend(
            base_url="ht!tp://bad url", model="m", api_key=None,
            transport=httpx.MockTransport(_stub_handler),
        )
        res = backend.invoke(make_call(), "x")
        assert res.ok is False
        assert res.error and "backend api" in res.error


# --------------------------------------------------------------------------
# replay_backends.py - RecordingBackend + ReplayBackend roundtrip
# --------------------------------------------------------------------------


class StubLiveBackend:
    """Minimal live backend for record/replay tests - no CLI, no network."""

    name = "stub"

    def __init__(self, response_text=CANNED, ok=True, error=None):
        self._text = response_text
        self._ok = ok
        self._error = error
        self.invocations = []

    def invoke(self, call, assembled_prompt):
        self.invocations.append(assembled_prompt)
        return bbase.AgentResult(
            ok=self._ok, output_text=self._text, backend=self.name,
            error=self._error, elapsed_seconds=0.01, tokens_in=10, tokens_out=5,
        )

    def healthcheck(self):
        return bbase.HealthReport(backend=self.name, ok=True)

    def estimate_cost(self, text):
        return bbase.CostEstimate(backend=self.name, estimated_tokens=1, method="chars/4")


class TestRecordReplay:
    def test_roundtrip(self, tmp_path):
        from coverageflow.backends import replay_backends as rp

        live = StubLiveBackend()
        rec = rp.RecordingBackend(live, record_dir=str(tmp_path))
        call = make_call(request_id="req-0009", role="extractor")
        res = rec.invoke(call, "ASSEMBLED-PROMPT-TEXT")
        assert res.ok, res.error
        assert res.raw_response_ref == "req-0009.json"

        record = json.loads((tmp_path / "req-0009.json").read_text())
        assert record["role"] == "extractor"
        assert record["request_id"] == "req-0009"
        assert record["response_text"] == CANNED
        assert record["backend"] == "stub"
        assert record["recorded_at"]
        assert record["prompt_path"], "recording must keep where the prompt text is stored"
        assert record["prompt_sha256"] == record["assembled_prompt_sha256"]
        import hashlib
        assert record["assembled_prompt_sha256"] == hashlib.sha256(
            "ASSEMBLED-PROMPT-TEXT".encode()
        ).hexdigest()
        assert record["candidate_id"] == call.candidate_id

        # stored prompt text is recoverable from the recording directory
        prompt_path = tmp_path / record["prompt_path"]
        assert prompt_path.read_text() == "ASSEMBLED-PROMPT-TEXT"

        # replay exactly: same role + same assembled prompt -> same answer
        replay = rp.ReplayBackend(replay_dir=str(tmp_path))
        res2 = replay.invoke(call, "ASSEMBLED-PROMPT-TEXT")
        assert res2.ok
        assert res2.output_text == CANNED
        assert res2.backend == "replay"

    def test_replay_match_is_role_and_prompt_hash_not_request_id(self, tmp_path):
        # an orchestrator retry rebinds request_id for a fresh dispatch, but
        # role+prompt-hash stays the identity: replay must still answer.
        from coverageflow.backends import replay_backends as rp

        call1 = make_call(request_id="req-1", role="ingest")
        call2 = make_call(request_id="req-2", role="ingest")
        rec = rp.RecordingBackend(StubLiveBackend(), record_dir=str(tmp_path))
        rec.invoke(call1, "SHARED-PROMPT")
        replay = rp.ReplayBackend(replay_dir=str(tmp_path))
        res = replay.invoke(call2, "SHARED-PROMPT")
        assert res.ok
        assert res.output_text == CANNED

    def test_replay_miss_is_hard_error(self, tmp_path):
        from coverageflow.backends import replay_backends as rp

        rec = rp.RecordingBackend(StubLiveBackend(), record_dir=str(tmp_path))
        rec.invoke(make_call(request_id="req-1", role="ingest"), "PROMPT-A")
        replay = rp.ReplayBackend(replay_dir=str(tmp_path))
        with pytest.raises(rp.ReplayMissError):
            replay.invoke(make_call(request_id="req-2", role="ingest"), "PROMPT-B")
        with pytest.raises(rp.ReplayMissError):
            replay.invoke(make_call(request_id="req-3", role="extractor"), "PROMPT-A")

    def test_failed_live_result_not_recorded(self, tmp_path):
        from coverageflow.backends import replay_backends as rp

        live = StubLiveBackend(ok=False, error="backend claude: exit code 3")
        rec = rp.RecordingBackend(live, record_dir=str(tmp_path))
        res = rec.invoke(make_call(request_id="req-1"), "PROMPT")
        assert res.ok is False
        assert res.raw_response_ref is None  # nothing recorded
        assert list(tmp_path.glob("*.json")) == []

    def test_non_object_recording_is_skipped_not_fatal(self, tmp_path):
        """A stray list/string JSON file in the replay dir must not crash."""
        from coverageflow.backends import replay_backends as rp

        (tmp_path / "a-list.json").write_text("[1, 2, 3]", encoding="utf-8")
        (tmp_path / "a-string.json").write_text('"nope"', encoding="utf-8")
        (tmp_path / "bad-fields.json").write_text(
            json.dumps({"role": 5, "assembled_prompt_sha256": None,
                        "response_text": "x"}), encoding="utf-8")
        backend = rp.ReplayBackend(str(tmp_path))
        assert backend.healthcheck().ok is False  # nothing usable loaded

    def test_replay_healthcheck(self, tmp_path):
        from coverageflow.backends import replay_backends as rp

        empty = rp.ReplayBackend(replay_dir=str(tmp_path))
        assert empty.healthcheck().ok is False
        rp.RecordingBackend(StubLiveBackend(), record_dir=str(tmp_path)).invoke(
            make_call(request_id="req-1"), "P"
        )
        loaded = rp.ReplayBackend(replay_dir=str(tmp_path))
        assert loaded.healthcheck().ok is True
        assert "1" in loaded.healthcheck().detail

    def test_replay_passes_through_recorded_backend_metadata(self, tmp_path):
        from coverageflow.backends import replay_backends as rp

        rec = rp.RecordingBackend(StubLiveBackend(), record_dir=str(tmp_path))
        rec.invoke(make_call(request_id="req-1", role="writer"), "PROMPT")
        replay = rp.ReplayBackend(replay_dir=str(tmp_path))
        res = replay.invoke(make_call(request_id="req-2", role="writer"), "PROMPT")
        assert res.ok
        assert res.raw_response_ref == "req-1.json"  # recording the answer came from


# --------------------------------------------------------------------------
# backends.creation: factory + auto-detection for the controller CLI (A2)
# --------------------------------------------------------------------------

class TestBackendFactory:
    def test_make_backend_claude_codex_kimi_api_replay(self):
        from coverageflow.backends import cli_backends as cli
        from coverageflow import backends as be

        assert isinstance(be.make_backend("claude"), cli.ClaudeBackend)
        assert isinstance(be.make_backend("codex"), cli.CodexBackend)
        assert isinstance(be.make_backend("kimi"), cli.KimiBackend)
        assert be.make_backend("api").name == "api"
        assert be.make_backend("replay", replay_dir="/tmp/somewhere").name == "replay"

    def test_make_backend_unknown_name(self):
        import pytest as _pytest

        from coverageflow import backends as be

        with _pytest.raises(ValueError):
            be.make_backend("does-not-exist")

    def test_detect_available_finds_fake_clis(self):
        from coverageflow import backends as be

        available = be.detect_available_backends()
        assert "claude" in available
        assert "codex" in available
        # kimi detection depends on its --help; the fake advertises -p
        assert "kimi" in available
