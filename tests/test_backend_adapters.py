"""Fixture-based tests for the Codex/Claude adapters and the USD cost cap.

No real CLI is ever called: stream parsing uses recorded output, command
construction is inspected via the fake CLIs' argv/stdin logs.
"""

from __future__ import annotations

import json
from pathlib import Path

from coverageflow.backends import cli_backends as cli
from coverageflow.budget import BudgetTracker
from coverageflow.dispatch import RoleCall, check_cost_cap
from tests.conftest import make_call

FIXTURE = Path(__file__).parent / "fixtures" / "backend_streams" / "codex_smoke.jsonl"


def test_codex_parses_real_stream_fixture():
    res = cli.CodexBackend()._normalize_output(FIXTURE.read_text())
    assert res.ok, res.error
    assert json.loads(res.output_text) == {"ok": True}
    assert res.tokens_in == 16108          # includes cached
    assert res.tokens_cached == 11520
    assert res.tokens_out == 9
    assert res.cost_usd is None


def test_codex_last_agent_message_wins():
    lines = [
        {"type": "item.completed", "item": {"type": "agent_message", "text": "first"}},
        {"type": "item.completed", "item": {"type": "command_execution", "text": "x"}},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "last"}},
        {"type": "turn.completed", "usage": {"input_tokens": 5, "output_tokens": 2}},
    ]
    res = cli.CodexBackend()._normalize_output("\n".join(map(json.dumps, lines)))
    assert res.output_text == "last"


def test_claude_envelope_sums_cache_tokens_and_captures_cost():
    env = {"type": "result", "is_error": False, "result": "{}",
           "total_cost_usd": 0.0421,
           "usage": {"input_tokens": 10, "cache_creation_input_tokens": 200,
                     "cache_read_input_tokens": 3000, "output_tokens": 50}}
    res = cli.ClaudeBackend()._normalize_output(json.dumps(env))
    assert res.ok
    assert res.tokens_in == 3210 and res.tokens_out == 50
    assert res.cost_usd == 0.0421
    assert res.tokens_cached == 3000


def test_claude_envelope_without_cost_is_none():
    env = {"result": "{}", "usage": {"input_tokens": 4, "output_tokens": 1}}
    res = cli.ClaudeBackend()._normalize_output(json.dumps(env))
    assert res.cost_usd is None and res.tokens_in == 4


def _argv(tmp_path, monkeypatch, backend, call=None):
    log = tmp_path / "argv.jsonl"
    monkeypatch.setenv("FAKE_ARGV_LOG", str(log))
    stdin_log = tmp_path / "stdin.txt"
    monkeypatch.setenv("FAKE_STDIN_LOG", str(stdin_log))
    res = backend.invoke(call or make_call(), "SECRET-PROMPT-XYZ")
    assert res.ok, res.error
    argv = json.loads(log.read_text().strip().splitlines()[-1])
    assert stdin_log.read_text() == "SECRET-PROMPT-XYZ"
    assert not any("SECRET-PROMPT-XYZ" in a for a in argv)
    return argv, res


def test_codex_command_flags_and_stdin(tmp_path, monkeypatch):
    argv, res = _argv(tmp_path, monkeypatch, cli.CodexBackend(model="gpt-test"))
    for flag in ("--skip-git-repo-check", "--ephemeral", "--ignore-user-config", "--json"):
        assert flag in argv
    assert argv[argv.index("--sandbox") + 1] == "read-only"
    assert argv[argv.index("--model") + 1] == "gpt-test"
    assert argv[-1] == "-"
    assert res.model == "gpt-test"


def test_codex_model_defaults_to_luna_high_and_env_overrides(tmp_path, monkeypatch):
    monkeypatch.delenv("CF_CODEX_MODEL", raising=False)
    monkeypatch.delenv("CF_CODEX_EFFORT", raising=False)
    argv, res = _argv(tmp_path, monkeypatch, cli.CodexBackend())
    assert argv[argv.index("--model") + 1] == "gpt-6-luna" and res.model == "gpt-6-luna"
    assert 'model_reasoning_effort="high"' in argv
    monkeypatch.setenv("CF_CODEX_MODEL", "env-model")
    assert cli.CodexBackend().model == "env-model"


def test_codex_output_schema_written_and_cleaned(tmp_path, monkeypatch):
    seen = {}
    real = cli._CliBackendBase._run

    def spy(self, args, timeout, workdir, stdin_text=None):
        path = Path(args[args.index("--output-schema") + 1])
        seen["path"], seen["text"] = path, path.read_text()
        return real(self, args, timeout, workdir, stdin_text=stdin_text)

    monkeypatch.setattr(cli._CliBackendBase, "_run", spy)
    res = cli.CodexBackend(json_schema={"type": "object"}).invoke(make_call(), "p")
    assert res.ok
    assert json.loads(seen["text"]) == {"type": "object"}
    assert not seen["path"].exists()


def test_claude_command_isolation_flags_and_stdin(tmp_path, monkeypatch):
    argv, res = _argv(tmp_path, monkeypatch, cli.ClaudeBackend(model="sonnet-x"))
    assert argv[argv.index("--tools") + 1] == ""
    assert argv[argv.index("--mcp-config") + 1] == '{"mcpServers":{}}'
    assert argv[argv.index("--setting-sources") + 1] == ""
    for flag in ("--strict-mcp-config", "--disable-slash-commands",
                 "--no-session-persistence", "--system-prompt"):
        assert flag in argv
    assert argv[argv.index("--model") + 1] == "sonnet-x"
    assert "--json-schema" not in argv and "--max-budget-usd" not in argv
    assert res.model == "sonnet-x"
    assert res.cost_usd == 0.0123


def test_claude_optional_schema_and_budget(tmp_path, monkeypatch):
    call = make_call()
    call = RoleCall(**{**call.__dict__, "budget": {
        **call.budget, "cost_usd_max": 0.5}})
    argv, _ = _argv(tmp_path, monkeypatch,
                    cli.ClaudeBackend(json_schema={"type": "object"}), call)
    assert json.loads(argv[argv.index("--json-schema") + 1]) == {"type": "object"}
    assert argv[argv.index("--max-budget-usd") + 1] == "0.5"


# ---------------------------------------------------------------- cost cap
def test_per_call_cost_cap():
    from coverageflow.backends.base import AgentResult
    call = make_call()
    over = AgentResult(ok=True, cost_usd=0.9)
    assert check_cost_cap(call, over) is None          # no cap: unlimited
    capped = RoleCall(**{**call.__dict__, "budget": {**call.budget, "cost_usd_max": 0.5}})
    assert "cost cap exceeded" in check_cost_cap(capped, over)
    assert check_cost_cap(capped, AgentResult(ok=True, cost_usd=0.1)) is None
    assert check_cost_cap(capped, AgentResult(ok=True, cost_usd=None)) is None


def test_case_cost_cap_stops_dispatch():
    t = BudgetTracker({"case_cost_usd_max": 1.0})
    assert t.check_dispatch("writer")[0]
    t.add_cost(0.6)
    t.add_cost(None)
    assert t.check_dispatch("writer")[0]
    t.add_cost(0.5)
    ok, reason = t.check_dispatch("writer")
    assert not ok and reason.code == "case_cost"
    assert not t.check_acceptance()[0]
    # survives a state round trip
    t2 = BudgetTracker(t.limits_state(), t.used_state())
    assert not t2.check_dispatch("writer")[0]


def test_cost_cap_default_unlimited():
    t = BudgetTracker({})
    t.add_cost(1e6)
    assert t.check_dispatch("writer")[0]


def test_codex_usage_limit_blocks_instead_of_spending_retries(monkeypatch):
    from coverageflow.backends import cli_backends as cb
    stream = "\n".join([
        '{"type":"thread.started","thread_id":"t"}',
        '{"type":"turn.started"}',
        '{"type":"error","message":"You\\u2019ve hit your usage limit. Try again at 3:52 AM."}',
        '{"type":"turn.failed","error":{"message":"You\\u2019ve hit your usage limit. Try again at 3:52 AM."}}',
    ])
    be = cb.CodexBackend(cli_path="codex")
    monkeypatch.setattr(be, "_run", lambda *a, **k: (stream, "", 1, False))
    monkeypatch.setattr(be, "_build_args", lambda *a, **k: (["exec", "-"], None))
    monkeypatch.setattr(be, "_resolve", lambda: "codex")
    res = be.invoke(make_call(), "prompt")
    assert not res.ok and res.blocking and res.block_code == "backend_unavailable"
    assert "usage limit" in res.error


def test_codex_error_event_message_is_extracted():
    from coverageflow.backends import cli_backends as cb
    be = cb.CodexBackend(cli_path="codex")
    out = '{"type":"turn.failed","error":{"message":"You have hit your usage limit."}}'
    assert be._error_from_stdout(out) == "You have hit your usage limit."
    assert cb._UNAVAILABLE_RE.search("You have hit your usage limit.")
    assert not cb._UNAVAILABLE_RE.search("schema validation failed")
