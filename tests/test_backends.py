"""Tests for the agent layer: backends (base, cli, api, replay).

Owner: stream A3. All tests run offline - no network, no real CLI logins.
Fake CLIs live under tests/fixtures/fakeclis/ and are placed on PATH.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from coverageflow.backends import base as bbase
from coverageflow.backends import cli_backends as cli
from conftest import FAKECLIS, make_call


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
        backend = make_backend(name)
        res = backend.invoke(make_call(role="extractor"), "PROMPT-MARKER-12345")
        assert res.ok, res.error
        lines = log.read_text().strip().splitlines()
        argvs = [json.loads(x) for x in lines if x.strip()]
        assert argvs, "CLI was never invoked"
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

    @pytest.mark.parametrize("name,expect_json", [("claude", True), ("codex", True), ("kimi", False)])
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
