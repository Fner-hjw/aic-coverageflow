"""Record/replay backends - offline test suite and recovery evidence.

Owner: stream A3. Contract (docs/contracts/interfaces.md section 6):

  replay/<case_id>/<request_id>.json:
    {"role","request_id","candidate_id","prompt_sha256","prompt_path",
     "assembled_prompt_sha256","response_text","backend","recorded_at"}

`RecordingBackend` wraps any live backend and writes one recording per
successful invocation (the assembled prompt text is stored under
``prompts/<request_id>.md`` inside the same directory so the exact prompt
bytes stay recoverable). `ReplayBackend` answers a call when *role +
assembled-prompt sha256* match a recording; a miss is a hard error raised as
`ReplayMissError` - never a silent replay of the wrong context.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
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

RECORD_FIELDS = (
    "role",
    "request_id",
    "candidate_id",
    "prompt_sha256",
    "prompt_path",
    "assembled_prompt_sha256",
    "response_text",
    "backend",
    "recorded_at",
)


def _utc_now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _atomic_write_json(path: Path, data: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(tmp, path)


def _atomic_write_text(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


class ReplayMissError(RuntimeError):
    """A replay call had no matching (role, assembled_prompt_sha256) recording.

    Hard error by design: tests and evidence runs must never silently replay a
    response that was recorded for different context. Regenerate recordings
    (via a recorded live run) if a prompt card or payload changed.
    """

    def __init__(self, role: str, prompt_sha256: str, available: int) -> None:
        super().__init__(
            f"ReplayMissError: no recording for role {role!r} with "
            f"assembled_prompt_sha256={prompt_sha256} "
            f"({available} recording(s) loaded); refusing to replay wrong "
            "context - rerun a recorded live session to refresh the replay set, "
            "or check that the prompt card / payload actually matches the "
            "recording run"
        )
        self.role = role
        self.prompt_sha256 = prompt_sha256
        self.recordings_available = available


class ReplayBackend:
    """Answers calls from recordings; used by the offline suite + evidence."""

    name = "replay"

    def __init__(self, replay_dir: str) -> None:
        self._dir = Path(replay_dir)
        # (role, assembled_prompt_sha256) -> record
        self._index: dict[tuple[str, str], dict] = {}
        self._load()

    def _load(self) -> None:
        self._index.clear()
        if not self._dir.is_dir():
            return
        for entry in sorted(self._dir.glob("*.json")):
            try:
                record = json.loads(entry.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue  # non-recording JSON in the dir: skip, not fatal
            if not all(f in record for f in RECORD_FIELDS):
                continue
            key = (record["role"], record["assembled_prompt_sha256"])
            # first recording wins on duplicate keys (ordered by filename)
            self._index.setdefault(key, record)

    # -- Backend protocol -----------------------------------------------------
    def healthcheck(self) -> HealthReport:
        return HealthReport(
            backend=self.name,
            ok=bool(self._index),
            detail=f"{len(self._index)} recording(s) in {self._dir}",
        )

    def invoke(self, call: "RoleCall", assembled_prompt: str) -> AgentResult:
        sha = _sha256_hex(assembled_prompt)
        record = self._index.get((call.role, sha))
        if record is None:
            raise ReplayMissError(call.role, sha, len(self._index))
        return AgentResult(
            ok=True,
            output_text=record["response_text"],
            backend=self.name,
            elapsed_seconds=0.0,
            tokens_in=estimate_tokens_chars_per_four(assembled_prompt),
            tokens_out=estimate_tokens_chars_per_four(record["response_text"]),
            raw_response_ref=record["request_id"] + ".json",
        )

    def estimate_cost(self, text: str) -> CostEstimate:
        return CostEstimate(
            backend=self.name,
            estimated_tokens=estimate_tokens_chars_per_four(text),
            method="chars/4",
            note="replay costs are zero; estimate kept for budget symmetry",
        )


class RecordingBackend:
    """Wraps any live backend; writes a replay recording per successful call."""

    name = "record"

    def __init__(self, wrapped, record_dir: str) -> None:
        self._wrapped = wrapped
        self._dir = Path(record_dir)

    @property
    def recordings_dir(self) -> Path:
        return self._dir

    def healthcheck(self) -> HealthReport:
        base = self._wrapped.healthcheck()
        detail = f"{base.detail}; recording into {self._dir}"
        return HealthReport(
            backend=self.name, ok=base.ok and self._dir.is_dir(), detail=detail
        )

    def invoke(self, call: "RoleCall", assembled_prompt: str) -> AgentResult:
        self._dir.mkdir(parents=True, exist_ok=True)
        result = self._wrapped.invoke(call, assembled_prompt)
        if not result.ok:
            # Errors carry no artifact schema to replay; record successes only.
            return result
        prompts_dir = self._dir / "prompts"
        prompts_dir.mkdir(parents=True, exist_ok=True)
        prompt_rel = f"prompts/{call.request_id}.md"
        prompt_abs = self._dir / prompt_rel
        _atomic_write_text(prompt_abs, assembled_prompt)

        prompt_sha = _sha256_hex(prompt_abs.read_text(encoding="utf-8"))
        record = {
            "role": call.role,
            "request_id": call.request_id,
            "candidate_id": call.candidate_id,
            "prompt_sha256": prompt_sha,
            "prompt_path": prompt_rel,
            "assembled_prompt_sha256": _sha256_hex(assembled_prompt),
            "response_text": result.output_text or "",
            "backend": self._wrapped.name,
            "recorded_at": _utc_now_iso(),
        }
        record_path = self._dir / f"{call.request_id}.json"
        _atomic_write_json(record_path, record)
        return AgentResult(
            ok=True,
            output_text=result.output_text,
            backend=result.backend,
            error=None,
            elapsed_seconds=result.elapsed_seconds,
            tokens_in=result.tokens_in,
            tokens_out=result.tokens_out,
            raw_response_ref=f"{call.request_id}.json",
        )

    def estimate_cost(self, text: str) -> CostEstimate | None:
        return self._wrapped.estimate_cost(text)
