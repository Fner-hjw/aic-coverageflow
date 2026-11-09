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
import shutil
import subprocess
import time
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


def _stderr_tail(stderr: str) -> str:
    stderr = (stderr or "").strip()
    if len(stderr) > _STDERR_TAIL_CHARS:
        stderr = stderr[-_STDERR_TAIL_CHARS:]
    return stderr


def _extract_usage(obj: object) -> tuple[int | None, int | None]:
    """Pull (input, output) token counts out of a parsed JSON envelope."""
    if not isinstance(obj, dict):
        return None, None
    usage = obj.get("usage")
    if not isinstance(usage, dict):
        return None, None
    inp = usage.get("input_tokens")
    out = usage.get("output_tokens")
    return (
        inp if isinstance(inp, int) else None,
        out if isinstance(out, int) else None,
    )


class _CliBackendBase:
    """Shared runner: timeout, non-zero exit, missing binary -> ok=False."""

    name = "cli"
    cli_binary: str = ""

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
        args = [path] + list(self._build_args(assembled_prompt))
        timeout = float(call.budget.get("timeout_seconds", 300.0))
        started = time.monotonic()
        try:
            proc = subprocess.run(
                args,
                input=None,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return AgentResult(
                ok=False,
                error=(
                    f"backend {self.name}: timeout after {timeout:g}s "
                    f"(budget timeout_seconds); process killed"
                ),
                backend=self.name,
                elapsed_seconds=time.monotonic() - started,
                tokens_in=estimate_tokens_chars_per_four(assembled_prompt),
            )
        except OSError as exc:  # not executable, permission, etc.
            return AgentResult(
                ok=False,
                error=f"backend {self.name}: cannot execute CLI: {exc}",
                backend=self.name,
                elapsed_seconds=time.monotonic() - started,
            )
        elapsed = time.monotonic() - started
        stdout, stderr = proc.stdout or "", proc.stderr or ""
        if proc.returncode != 0:
            return AgentResult(
                ok=False,
                error=(
                    f"backend {self.name}: exit code {proc.returncode}; "
                    f"stderr tail: {_stderr_tail(stderr) or '(empty)'}"
                ),
                backend=self.name,
                elapsed_seconds=elapsed,
            )
        result = self._normalize_output(stdout)
        result.elapsed_seconds = elapsed
        result.tokens_in = result.tokens_in or estimate_tokens_chars_per_four(
            assembled_prompt
        )
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
    def _build_args(self, prompt: str) -> list[str]:
        raise NotImplementedError

    def _normalize_output(self, stdout: str) -> AgentResult:
        raise NotImplementedError

    # -- Internals ----------------------------------------------------------
    def _resolve(self) -> str | None:
        if self._cli_path_override is not None:
            return (
                self._cli_path_override
                if os.path.exists(self._cli_path_override)
                else None
            )
        return shutil.which(self.cli_binary)


class ClaudeBackend(_CliBackendBase):
    """`claude -p <prompt> --output-format json` (fresh invocation per call)."""

    name = "claude"
    cli_binary = "claude"

    def _build_args(self, prompt: str) -> list[str]:
        return ["-p", prompt, "--output-format", "json"]

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
        return AgentResult(
            ok=True,
            output_text=envelope["result"],
            backend=self.name,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
        )


class CodexBackend(_CliBackendBase):
    """`codex exec <prompt> --json` (NDJSON event stream on stdout)."""

    name = "codex"
    cli_binary = "codex"

    def _build_args(self, prompt: str) -> list[str]:
        return ["exec", prompt, "--json"]

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
            if obj.get("type") == "error":
                err_obj = obj
                continue
            item = obj.get("item")
            if (
                isinstance(item, dict)
                and item.get("item_type") == "agent_message"
                and isinstance(item.get("text"), str)
            ):
                message = item["text"]  # last agent message wins
            if isinstance(obj.get("usage"), dict):
                usage = obj["usage"]  # last usage record wins
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
        tokens_in, tokens_out = _extract_usage({"usage": usage or {}})
        return AgentResult(
            ok=True,
            output_text=message,
            backend=self.name,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
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
        """Detect a -p style headless invocation from `kimi --help` output."""
        if self._detected_flags is not None:
            return self._detected_flags
        self._detected_flags = []
        path = self._resolve()
        if path is None:
            return self._detected_flags
        try:
            proc = subprocess.run(
                [path, "--help"], capture_output=True, text=True, timeout=30
            )
        except (OSError, subprocess.TimeoutExpired):
            return self._detected_flags
        help_text = proc.stdout or ""
        if "-p" in help_text or "--prompt" in help_text:
            self._detected_flags = ["-p"]
        return self._detected_flags

    def _build_args(self, prompt: str) -> list[str]:
        return ["-p", prompt]

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
