"""Headless CLI backend adapters: claude, codex, kimi.

Owner: stream A3. Each adapter is a *fresh invocation per call* (no session
reuse), enforces the per-call timeout from RoleCall.budget, captures stderr,
and normalizes output into AgentResult. Best-effort token estimation is
chars/4 (base.estimate_tokens_chars_per_four) unless the CLI reports real
usage in its JSON envelope.

Tests exercise these through fake CLI executables (tests/fixtures/fakeclis/)
- never a real CLI or login.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

from .base import (
    AgentResult,
    CostEstimate,
    HealthReport,
    estimate_tokens_chars_per_four,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..dispatch import RoleCall

_STDERR_TAIL_CHARS = 2000

# The harness kill lands this long after the CLI's own --time-limit, so the
# CLI gets a chance to stop itself and write its export before being killed.
_GRACE_SECONDS = 20.0

# An option token, not a substring: "-p" must not match "--port" or "--profile".
_HEADLESS_FLAG_RE = re.compile(r"(?:^|[\s,])(-p|--prompt)(?=[\s,=]|$)", re.MULTILINE)


def _stderr_tail(stderr: str) -> str:
    stderr = (stderr or "").strip()
    if len(stderr) > _STDERR_TAIL_CHARS:
        stderr = stderr[-_STDERR_TAIL_CHARS:]
    return stderr


def _extract_usage(obj: object) -> tuple[int | None, int | None]:
    """Pull (input, output) token counts out of a parsed JSON envelope.

    Input counts every input token the model saw: fresh input plus prompt
    cache writes plus prompt cache reads (the claude envelope reports them
    separately; summing is a no-op for envelopes without cache fields).
    """
    if not isinstance(obj, dict):
        return None, None
    usage = obj.get("usage")
    if not isinstance(usage, dict):
        return None, None
    inp = usage.get("input_tokens")
    out = usage.get("output_tokens")
    if not isinstance(inp, int):
        return None, out if isinstance(out, int) else None
    for key in ("cache_creation_input_tokens", "cache_read_input_tokens"):
        extra = usage.get(key)
        if isinstance(extra, int):
            inp += extra
    return inp, out if isinstance(out, int) else None


class _TempFiles:
    """Cleanup handle for several temp files (the runner calls ``unlink``)."""

    def __init__(self, *paths: Path) -> None:
        self._paths = paths

    def unlink(self) -> None:
        for path in self._paths:
            try:
                path.unlink()
            except OSError:
                pass


def _write_temp(text: str, prefix: str, suffix: str) -> Path:
    fd, name = tempfile.mkstemp(prefix=prefix, suffix=suffix)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
    return Path(name)


def _schema_text(schema: object) -> str | None:
    if schema is None:
        return None
    return schema if isinstance(schema, str) else json.dumps(schema)


class _CliBackendBase:
    """Shared runner: timeout, non-zero exit, missing binary -> ok=False.

    Output is captured incrementally so a call that runs out of time still
    hands back everything the CLI wrote. Exit codes in ``soft_exit_codes``
    mean "the CLI stopped itself but produced output" (e.g. its own
    ``--time-limit``); that output is normalized instead of discarded.
    """

    name = "cli"
    cli_binary: str = ""
    soft_exit_codes: frozenset[int] = frozenset()
    # When True the prompt is written to the child's stdin (never argv);
    # _build_args then must not include it.
    prompt_via_stdin: bool = False
    model: str | None = None

    def __init__(self, cli_path: str | None = None) -> None:
        self._cli_path_override = cli_path

    # -- Backend protocol ---------------------------------------------------
    def healthcheck(self) -> HealthReport:
        path = self._resolve()
        if path is None:
            return HealthReport(
                backend=self.name,
                ok=False,
                detail=(
                    f"{self.cli_binary} not found "
                    "(installed and on PATH?)"
                ),
            )
        return HealthReport(
            backend=self.name, ok=True, detail=f"{self.cli_binary} at {path}"
        )

    def invoke(self, call: "RoleCall", assembled_prompt: str) -> AgentResult:
        path = self._resolve()
        if path is None:
            return AgentResult(
                ok=False,
                error=(
                    f"backend {self.name}: CLI binary {self.cli_binary!r} "
                    "not found (installed and on PATH?)"
                ),
                backend=self.name,
            )
        timeout = float(call.budget.get("timeout_seconds", 300.0))
        workdir = getattr(call, "workdir", None)
        started = time.monotonic()
        tokens_in = estimate_tokens_chars_per_four(assembled_prompt)
        cleanup: Path | None = None
        try:
            prompt_args, cleanup = self._build_args(assembled_prompt, call, timeout)
            args = [path] + list(prompt_args)
            stdout, stderr, returncode, timed_out = self._run(
                args, timeout, workdir,
                stdin_text=assembled_prompt if self.prompt_via_stdin else None)
        except OSError as exc:  # not executable, permission, etc.
            return AgentResult(
                ok=False,
                error=f"backend {self.name}: cannot execute CLI: {exc}",
                backend=self.name,
                elapsed_seconds=time.monotonic() - started,
            )
        finally:
            if cleanup is not None:
                try:
                    cleanup.unlink()
                except OSError:
                    pass
        elapsed = time.monotonic() - started
        tokens_out = estimate_tokens_chars_per_four(stdout) if stdout else None

        if timed_out:
            return AgentResult(
                ok=False,
                output_text=stdout or None,
                error=(
                    f"backend {self.name}: timeout after {timeout:g}s "
                    f"(budget timeout_seconds); process group killed after "
                    f"{len(stdout)} chars of partial output"
                ),
                backend=self.name,
                elapsed_seconds=elapsed,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
            )

        if returncode != 0:
            if returncode in self.soft_exit_codes and stdout.strip():
                # The CLI stopped itself but still wrote output: normalize it
                # so a complete artifact is accepted rather than thrown away.
                result = self._normalize_output(stdout)
                result.elapsed_seconds = elapsed
                result.tokens_in = result.tokens_in or tokens_in
                result.tokens_out = result.tokens_out or estimate_tokens_chars_per_four(
                    result.output_text or "")
                if result.ok:
                    result.error = (
                        f"backend {self.name}: CLI stopped early "
                        f"(exit {returncode}) but produced a complete artifact"
                    )
                return result
            return AgentResult(
                ok=False,
                output_text=stdout or None,
                error=(
                    f"backend {self.name}: exit code {returncode}; "
                    f"stderr tail: {_stderr_tail(stderr) or '(empty)'}"
                ),
                backend=self.name,
                elapsed_seconds=elapsed,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
            )

        result = self._normalize_output(stdout)
        result.model = result.model or self.model
        result.elapsed_seconds = elapsed
        result.tokens_in = result.tokens_in or tokens_in
        result.tokens_out = result.tokens_out or estimate_tokens_chars_per_four(
            result.output_text or ""
        )
        return result

    def estimate_cost(self, text: str) -> CostEstimate:
        return CostEstimate(
            backend=self.name,
            estimated_tokens=estimate_tokens_chars_per_four(text),
            method="chars/4",
            note="pre-invoke estimate; real usage surfaced when the CLI reports it",
        )

    # -- Hooks --------------------------------------------------------------
    def _build_args(self, prompt: str, call: "RoleCall",
                    timeout: float) -> tuple[list[str], Path | None]:
        """Argv fragment carrying the prompt, plus an optional temp file to drop."""
        raise NotImplementedError

    def _normalize_output(self, stdout: str) -> AgentResult:
        raise NotImplementedError

    # -- Internals ----------------------------------------------------------
    def _run(self, args: list[str], timeout: float,
             workdir: str | None,
             stdin_text: str | None = None) -> tuple[str, str, int, bool]:
        """Run *args*, capturing stdout/stderr incrementally.

        Returns ``(stdout, stderr, returncode, timed_out)``. On deadline the
        whole process group is killed, but everything already written is
        returned: a role that produced most of its artifact is diagnosable
        instead of invisible.
        """
        proc = subprocess.Popen(
            args,
            stdin=subprocess.DEVNULL if stdin_text is None else subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=workdir,
            start_new_session=True,
        )
        out: list[str] = []
        err: list[str] = []

        def _drain(stream, sink: list[str]) -> None:
            try:
                for line in stream:
                    sink.append(line)
            except (ValueError, OSError):
                pass
            finally:
                try:
                    stream.close()
                except OSError:
                    pass

        readers = [
            threading.Thread(target=_drain, args=(proc.stdout, out), daemon=True),
            threading.Thread(target=_drain, args=(proc.stderr, err), daemon=True),
        ]
        if stdin_text is not None:
            def _feed() -> None:
                try:
                    proc.stdin.write(stdin_text)
                except (BrokenPipeError, ValueError, OSError):
                    pass
                finally:
                    try:
                        proc.stdin.close()
                    except (BrokenPipeError, ValueError, OSError):
                        pass

            readers.append(threading.Thread(target=_feed, daemon=True))
        for reader in readers:
            reader.start()

        timed_out = False
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            self._kill_group(proc)
        for reader in readers:
            reader.join(timeout=5)
        return "".join(out), "".join(err), proc.returncode, timed_out

    @staticmethod
    def _kill_group(proc: subprocess.Popen) -> None:
        """Kill the whole process group: CLI wrappers spawn their own children."""
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                proc.kill()
            except OSError:
                pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass

    def _resolve(self) -> str | None:
        if self._cli_path_override is not None:
            return (
                self._cli_path_override
                if os.path.exists(self._cli_path_override)
                else None
            )
        return shutil.which(self.cli_binary)


class ClaudeBackend(_CliBackendBase):
    """`claude -p` single-shot JSON emitter (fresh, isolated invocation).

    Config: ``model`` ctor arg, else env ``CF_CLAUDE_MODEL``, else "sonnet";
    ``effort`` / ``CF_CLAUDE_EFFORT`` (default "medium"); ``json_schema``
    (dict or JSON string) or ``call.budget["json_schema"]``;
    ``max_budget_usd`` or ``call.budget["cost_usd_max"]``.
    """

    name = "claude"
    cli_binary = "claude"
    prompt_via_stdin = True

    SYSTEM_PROMPT = (
        "You are a single-shot JSON emitter inside an automated harness. "
        "Answer in one reply with exactly one JSON object and nothing else. "
        "Do not use tools or ask questions."
    )

    def __init__(self, cli_path: str | None = None, model: str | None = None,
                 effort: str | None = None, json_schema: object = None,
                 max_budget_usd: float | None = None) -> None:
        super().__init__(cli_path)
        self.model = model or os.environ.get("CF_CLAUDE_MODEL") or "sonnet"
        self.effort = effort or os.environ.get("CF_CLAUDE_EFFORT") or "medium"
        self.json_schema = json_schema
        self.max_budget_usd = max_budget_usd

    def _build_args(self, prompt: str, call: "RoleCall",
                    timeout: float) -> tuple[list[str], Path | None]:
        budget = getattr(call, "budget", None) or {}
        args = [
            "-p",
            "--model", self.model,
            "--effort", self.effort,
            "--output-format", "json",
            "--tools", "",
            "--strict-mcp-config",
            "--mcp-config", '{"mcpServers":{}}',
            "--setting-sources", "",
            "--disable-slash-commands",
            "--no-session-persistence",
            "--system-prompt", self.SYSTEM_PROMPT,
        ]
        schema = _schema_text(budget.get("json_schema") or self.json_schema)
        if schema:
            args += ["--json-schema", schema]
        cap = budget.get("cost_usd_max")
        if cap is None:
            cap = self.max_budget_usd
        if cap is not None:
            args += ["--max-budget-usd", f"{float(cap):g}"]
        return args, None

    def _normalize_output(self, stdout: str) -> AgentResult:
        try:
            envelope = json.loads(stdout)
        except json.JSONDecodeError as exc:
            return AgentResult(
                ok=False,
                error=(
                    f"backend claude: could not parse stdout as JSON "
                    f"(--output-format json expected): {exc}"
                ),
                backend=self.name,
            )
        if not isinstance(envelope, dict) or not isinstance(
            envelope.get("result"), str
        ):
            return AgentResult(
                ok=False,
                error="backend claude: JSON envelope missing string 'result' field",
                backend=self.name,
            )
        if envelope.get("is_error"):
            return AgentResult(
                ok=False,
                error=f"backend claude: CLI reported error result: {envelope['result'][:500]}",
                backend=self.name,
            )
        tokens_in, tokens_out = _extract_usage(envelope)
        cost = envelope.get("total_cost_usd")
        usage = envelope.get("usage") if isinstance(envelope.get("usage"), dict) else {}
        cached = usage.get("cache_read_input_tokens")
        return AgentResult(
            ok=True,
            output_text=envelope["result"],
            backend=self.name,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_usd=float(cost) if isinstance(cost, (int, float))
            and not isinstance(cost, bool) else None,
            tokens_cached=cached if isinstance(cached, int) else None,
        )


class CodexBackend(_CliBackendBase):
    """`codex exec --json` (fresh, read-only, ephemeral); prompt on stdin.

    Config: ``model`` ctor arg, else env ``CF_CODEX_MODEL``, else None (the
    CLI's own default; ``--model`` omitted). ``reasoning_effort`` /
    ``CF_CODEX_EFFORT`` likewise. ``json_schema`` (dict or JSON string) or
    ``call.budget["json_schema"]`` is passed as ``--output-schema <file>``.
    """

    name = "codex"
    cli_binary = "codex"
    prompt_via_stdin = True

    def __init__(self, cli_path: str | None = None, model: str | None = None,
                 reasoning_effort: str | None = None,
                 json_schema: object = None) -> None:
        super().__init__(cli_path)
        self.model = model or os.environ.get("CF_CODEX_MODEL") or None
        self.reasoning_effort = (
            reasoning_effort or os.environ.get("CF_CODEX_EFFORT") or None)
        self.json_schema = json_schema

    def _build_args(self, prompt: str, call: "RoleCall",
                    timeout: float) -> tuple[list[str], Path | None]:
        budget = getattr(call, "budget", None) or {}
        args = [
            "exec", "--json",
            "--skip-git-repo-check",
            "--sandbox", "read-only",
            "--ephemeral",
            "--ignore-user-config",
        ]
        if self.model:
            args += ["--model", self.model]
        if self.reasoning_effort:
            args += ["--config", f'model_reasoning_effort="{self.reasoning_effort}"']
        cleanup: _TempFiles | None = None
        schema = _schema_text(budget.get("json_schema") or self.json_schema)
        if schema:
            path = _write_temp(schema, "cf-schema-", ".json")
            args += ["--output-schema", str(path)]
            cleanup = _TempFiles(path)
        args.append("-")  # prompt is read from stdin, never argv
        return args, cleanup

    def _normalize_output(self, stdout: str) -> AgentResult:
        message: str | None = None
        usage: dict | None = None
        err_obj = None
        parsed_lines = 0
        for line in stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            parsed_lines += 1
            if not isinstance(obj, dict):
                continue
            kind = obj.get("type")
            if kind in ("error", "turn.failed"):
                err_obj = obj
                continue
            item = obj.get("item")
            if (
                kind == "item.completed"
                and isinstance(item, dict)
                and item.get("type") == "agent_message"
                and isinstance(item.get("text"), str)
            ):
                message = item["text"]  # last agent message wins
            if kind == "turn.completed" and isinstance(obj.get("usage"), dict):
                usage = obj["usage"]  # last turn.completed wins
        if err_obj is not None and message is None:
            return AgentResult(
                ok=False,
                error=f"backend codex: stream reported error event: {json.dumps(err_obj)[:500]}",
                backend=self.name,
            )
        if message is None:
            return AgentResult(
                ok=False,
                error=(
                    f"backend codex: could not parse output as JSON events "
                    f"({parsed_lines} JSON lines, no agent message found)"
                ),
                backend=self.name,
            )
        usage = usage or {}
        tokens_in = usage.get("input_tokens")
        tokens_out = usage.get("output_tokens")
        cached = usage.get("cached_input_tokens")
        return AgentResult(
            ok=True,
            output_text=message,
            backend=self.name,
            tokens_in=tokens_in if isinstance(tokens_in, int) else None,
            tokens_out=tokens_out if isinstance(tokens_out, int) else None,
            tokens_cached=cached if isinstance(cached, int) else None,
        )


class KimiBackend(_CliBackendBase):
    """Headless kimi invocation; flags detected at runtime from `kimi --help`.

    Supports a -p/--prompt style invocation printing to stdout. Degrades with
    a clear error (never a guess-run) when the installed CLI has no headless
    mode.
    """

    name = "kimi"
    cli_binary = "kimi"

    def __init__(self, cli_path: str | None = None) -> None:
        super().__init__(cli_path)
        self._detected_flags: list[str] | None = None

    def invalidate_detection(self) -> None:
        """Force re-detection of headless flags on the next call."""
        self._detected_flags = None

    def _detect_headless_flags(self) -> list[str]:
        """Detect a -p style headless invocation from `kimi --help` output.

        The probe result is cached only on success: a transient failure (cold
        start past the probe timeout, momentarily unreadable binary) must not
        poison the instance for the rest of the session.
        """
        if self._detected_flags is not None:
            return self._detected_flags
        path = self._resolve()
        if path is None:
            return []
        try:
            proc = subprocess.run(
                [path, "--help"], capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired):
            return []  # not cached: the next call retries the probe
        match = _HEADLESS_FLAG_RE.search(proc.stdout or "")
        if match is None:
            self._detected_flags = []
        else:
            self._detected_flags = [match.group(1)]
        return self._detected_flags

    def _build_args(self, prompt: str, call: "RoleCall",
                    timeout: float) -> tuple[list[str], Path | None]:
        flag = (self._detected_flags or ["-p"])[0]
        return [flag, prompt], None

    def healthcheck(self) -> HealthReport:
        report = super().healthcheck()
        if not report.ok:
            return report
        if not self._detect_headless_flags():
            return HealthReport(
                backend=self.name,
                ok=False,
                detail=(
                    "kimi found but no headless prompt mode "
                    "(no -p/--prompt in `kimi --help` output); "
                    "upgrade the CLI or choose another backend"
                ),
            )
        return report

    def invoke(self, call: "RoleCall", assembled_prompt: str) -> AgentResult:
        if self._resolve() is None:
            return AgentResult(
                ok=False,
                error=(
                    f"backend {self.name}: CLI binary {self.cli_binary!r} "
                    "not found (installed and on PATH?)"
                ),
                backend=self.name,
            )
        if not self._detect_headless_flags():
            return AgentResult(
                ok=False,
                error=(
                    "backend kimi: installed CLI does not support headless "
                    "-p style invocation (detected from `kimi --help`); "
                    "cannot run role calls non-interactively"
                ),
                backend=self.name,
            )
        # kimi writes the response to stdout as plain text; no JSON envelope.
        return super().invoke(call, assembled_prompt)

    def _normalize_output(self, stdout: str) -> AgentResult:
        text = (stdout or "").strip("\n")
        if not text.strip():
            return AgentResult(
                ok=False, error="backend kimi: empty stdout", backend=self.name
            )
        return AgentResult(ok=True, output_text=text, backend=self.name)


class VibeBackend(_CliBackendBase):
    """`vibe --prompt-file <path> --output text` — headless Mistral Vibe CLI.

    The Mistral CLI runs in programmatic mode: one fresh invocation per role
    call, answer printed to stdout as plain text, exit code reflects success.
    Uses the CLI's own login (no API key handling here).

    The prompt travels in a file rather than argv (role prompts embed whole
    statement slices), the case directory is passed as ``--workdir`` so the
    payload's relative ``inputs/...`` references resolve, and ``--time-limit``
    lets the CLI stop itself and write its export (exit 3) before the harness
    kill lands.
    """

    name = "vibe"
    cli_binary = "vibe"
    soft_exit_codes = frozenset({3})  # CLI's own --time-limit stop

    # Roles that must read the case directory to do their job. Everything
    # else gets a self-contained prompt and runs single-shot: Vibe is an
    # agentic CLI, and left to itself it spends its turns listing directories
    # and reading files, turning one role call into a five-to-seven minute
    # exploration instead of a completion.
    #
    # Ingest is not here: its card says it does not read whole filings, and
    # the request now carries the slicer's anchor candidates. With tools it
    # opened all twelve documents and hit the CLI's own deadline every round
    # without ever emitting output.
    TOOL_ROLES = frozenset()

    # Replaces Vibe's own agent system prompt. Without this the model is told
    # it is an agent that inspects and edits a workspace — and it does.
    SYSTEM_PROMPT = (
        "You are a single-shot JSON emitter inside an automated harness.\n"
        "Answer the user message directly in one reply.\n"
        "Do not use tools. Do not inspect the filesystem. Do not ask questions.\n"
        "Do not write files. Do not explain.\n"
        "Emit exactly one JSON object and nothing else.\n"
    )

    def _build_args(self, prompt: str, call: "RoleCall",
                    timeout: float) -> tuple[list[str], Path | None]:
        fd, name = tempfile.mkstemp(prefix="cf-prompt-", suffix=".md")
        path = Path(name)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(prompt)
        args = ["--prompt-file", str(path), "--output", "text",
                "--max-price", "1.5"]  # hard per-call cost cap
        workdir = getattr(call, "workdir", None)
        if workdir:
            args += ["--workdir", str(workdir)]
        cleanup: Path | None = path
        if getattr(call, "role", None) not in self.TOOL_ROLES:
            sys_fd, sys_name = tempfile.mkstemp(prefix="cf-sys-", suffix=".md")
            with os.fdopen(sys_fd, "w", encoding="utf-8") as handle:
                handle.write(self.SYSTEM_PROMPT)
            args += ["--system-prompt-file", sys_name,
                     "--enabled-tools", "__none__",   # disables every tool
                     "--max-turns", "1"]
            cleanup = _TempFiles(path, Path(sys_name))
        # leave the CLI room to stop itself and export before the harness kill
        args += ["--time-limit", f"{max(30.0, timeout - _GRACE_SECONDS):g}"]
        return args, cleanup

    def _normalize_output(self, stdout: str) -> AgentResult:
        text = (stdout or "").strip("\n")
        if not text.strip():
            return AgentResult(
                ok=False, error="backend vibe: empty stdout", backend=self.name
            )
        return AgentResult(ok=True, output_text=text, backend=self.name)
