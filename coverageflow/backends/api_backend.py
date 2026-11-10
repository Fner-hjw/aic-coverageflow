"""OpenAI-compatible chat-completions API backend.

Owner: stream A3. Configuration resolution order (contracts interfaces s2):
explicit constructor args -> COVERAGEFLOW_BASE_URL / COVERAGEFLOW_MODEL /
COVERAGEFLOW_API_KEY env vars -> config file (JSON, {"api": {...}}). Retries
5xx and transport timeouts with exponential backoff (up to 3 attempts total);
4xx responses are not retried. Real token usage from the response `usage`
block is surfaced when present.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

import httpx

from .base import AgentResult, CostEstimate, HealthReport, estimate_tokens_chars_per_four

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..dispatch import RoleCall

ENV_BASE_URL = "COVERAGEFLOW_BASE_URL"
ENV_MODEL = "COVERAGEFLOW_MODEL"
ENV_API_KEY = "COVERAGEFLOW_API_KEY"
ENV_CONFIG = "COVERAGEFLOW_CONFIG"
DEFAULT_CONFIG_FILENAME = ".coverageflow.json"

_MAX_ATTEMPTS = 3  # total attempts: 1 + 2 retries on 5xx/timeout
_INITIAL_BACKOFF_SECONDS = 0.5


class ApiBackend:
    """`POST {base_url}/chat/completions`, one fresh call per role dispatch."""

    name = "api"

    def __init__(
        self,
        base_url: str | None = None,
        model: str | None = None,
        api_key: str | None = None,
        config_path: str | None = None,
        *,
        default_timeout: float = 120.0,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._explicit = {"base_url": base_url, "model": model, "api_key": api_key}
        self._config_path = config_path
        self._default_timeout = default_timeout
        self._transport = transport
        self._sleep = sleep

    # -- configuration ------------------------------------------------------
    def _load_config_file(self) -> dict:
        candidates = []
        if self._config_path is not None:
            candidates.append(Path(self._config_path))
        else:
            env_cfg = os.environ.get(ENV_CONFIG)
            if env_cfg:
                candidates.append(Path(env_cfg))
            candidates.append(Path(os.getcwd()) / DEFAULT_CONFIG_FILENAME)
        for path in candidates:
            if path.is_file():
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                if isinstance(data, dict) and isinstance(data.get("api"), dict):
                    return data["api"]
        return {}

    def _config(self) -> dict[str, str | None]:
        cfg_file = self._load_config_file()
        # The workspace UI configuration is an explicit workspace choice.
        workspace_cfg = {}
        workspace = os.environ.get("COVERAGEFLOW_WORKSPACE")
        if workspace and self._config_path is None:
            try:
                data = json.loads((Path(workspace) / ".coverageflow-api.json").read_text(encoding="utf-8"))
                if isinstance(data, dict) and isinstance(data.get("api"), dict):
                    workspace_cfg = data["api"]
            except (OSError, json.JSONDecodeError):
                pass
        return {
            "base_url": self._explicit["base_url"]
            or workspace_cfg.get("base_url")
            or os.environ.get(ENV_BASE_URL)
            or cfg_file.get("base_url"),
            "model": self._explicit["model"]
            or workspace_cfg.get("model")
            or os.environ.get(ENV_MODEL)
            or cfg_file.get("model"),
            "api_key": self._explicit["api_key"]
            or workspace_cfg.get("api_key")
            or os.environ.get(ENV_API_KEY)
            or cfg_file.get("api_key"),
        }

    def _configured(self) -> bool:
        # api_key optional for local OpenAI-compatible servers
        cfg = self._config()
        return bool(cfg["base_url"]) and bool(cfg["model"])

    # -- Backend protocol ----------------------------------------------------
    def healthcheck(self) -> HealthReport:
        cfg = self._config()
        if not cfg["base_url"] or not cfg["model"]:
            return HealthReport(
                backend=self.name,
                ok=False,
                detail=(
                    "API backend not configured: set base_url and model "
                    f"via constructor, {ENV_BASE_URL}/{ENV_MODEL} env vars, "
                    "or the config file"
                ),
            )
        return HealthReport(
            backend=self.name,
            ok=True,
            detail=f"configured: {cfg['base_url']} model={cfg['model']}",
        )

    def invoke(self, call: "RoleCall", assembled_prompt: str) -> AgentResult:
        cfg = self._config()
        if not cfg["base_url"] or not cfg["model"]:
            return AgentResult(
                ok=False,
                error="backend api: not configured (base_url/model missing)",
                backend=self.name,
                tokens_in=estimate_tokens_chars_per_four(assembled_prompt),
            )
        url = cfg["base_url"].rstrip("/") + "/chat/completions"
        headers = {"Authorization": f"Bearer {cfg['api_key']}"} if cfg["api_key"] else {}
        body = {
            "model": cfg["model"],
            "messages": [{"role": "user", "content": assembled_prompt}],
        }
        timeout = float(call.budget.get("timeout_seconds", self._default_timeout))
        started = time.monotonic()
        # timeout_seconds is the budget for the whole call, not per attempt:
        # three 240s attempts would blow a 240s budget three times over.
        deadline = started + timeout
        last_error = "no attempt made"
        attempts = 0
        with httpx.Client(transport=self._transport) as client:
            for attempt in range(1, _MAX_ATTEMPTS + 1):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    last_error = (
                        f"budget timeout_seconds={timeout:g} exhausted after "
                        f"{attempts} attempt(s)"
                    )
                    break
                attempts = attempt
                failed, error = _post_once(client, url, headers, body, remaining)
                if failed is None:  # success
                    return _normalize(error, self.name, started, assembled_prompt)
                last_error = error
                if not failed:  # non-retryable (4xx, malformed payload)
                    break
                if attempt < _MAX_ATTEMPTS:
                    backoff = _INITIAL_BACKOFF_SECONDS * (2 ** (attempt - 1))
                    if time.monotonic() + backoff >= deadline:
                        break
                    self._sleep(backoff)
        return AgentResult(
            ok=False,
            error=(
                f"backend api: {last_error} "
                f"(attempts={attempts}, endpoint={url})"
            ),
            backend=self.name,
            elapsed_seconds=time.monotonic() - started,
            tokens_in=estimate_tokens_chars_per_four(assembled_prompt),
        )

    def estimate_cost(self, text: str) -> CostEstimate:
        return CostEstimate(
            backend=self.name,
            estimated_tokens=estimate_tokens_chars_per_four(text),
            method="chars/4",
            note="pre-invoke estimate; usage block surfaced when reported",
        )


def _post_once(
    client: httpx.Client,
    url: str,
    headers: dict[str, str],
    body: dict[str, Any],
    timeout: float,
) -> tuple[bool | None, Any]:
    """One POST. Returns (None, response_json) on success or
    (retryable, error_descriptor) on failure.
    """
    try:
        resp = client.post(url, json=body, headers=headers, timeout=timeout)
    except (httpx.HTTPError, httpx.InvalidURL, ValueError) as exc:
        # HTTPError covers TimeoutException/TransportError plus the rest of the
        # hierarchy. InvalidURL is not an HTTPError, and a malformed base_url
        # surfaces as a bare ValueError from urllib inside httpx's cookie
        # handling - both would otherwise escape invoke() as a crash.
        return (True, f"request failed with {type(exc).__name__}: {exc}")
    if resp.status_code >= 500:
        return (True, f"HTTP {resp.status_code}: {resp.text[:300]}")
    if resp.status_code >= 400:
        return (False, f"HTTP {resp.status_code}: {resp.text[:300]}")
    try:
        data = resp.json()
    except (ValueError, json.JSONDecodeError):
        return (False, "HTTP 200 but body is not JSON")
    return (None, data)


def _normalize(data: Any, name: str, started: float, prompt: str = "") -> AgentResult:
    elapsed = time.monotonic() - started
    if not isinstance(data, dict) or not isinstance(data.get("choices"), list) or not data["choices"]:
        return AgentResult(
            ok=False,
            error="backend api: response missing 'choices' array",
            backend=name,
            elapsed_seconds=elapsed,
            tokens_in=estimate_tokens_chars_per_four(prompt),
        )
    first = data["choices"][0]
    # choices[0] and choices[0].message are server-controlled: either can be
    # null or a non-object, and .get() on those raises AttributeError.
    message = first.get("message") if isinstance(first, dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, str):
        return AgentResult(
            ok=False,
            error="backend api: choices[0].message.content missing or not a string",
            backend=name,
            elapsed_seconds=elapsed,
            tokens_in=estimate_tokens_chars_per_four(prompt),
        )
    usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
    tokens_in = usage.get("prompt_tokens") if isinstance(usage.get("prompt_tokens"), int) else None
    tokens_out = (
        usage.get("completion_tokens") if isinstance(usage.get("completion_tokens"), int) else None
    )
    # Local OpenAI-compatible servers often omit `usage` entirely. Dropping
    # tokens_in to None there loses the only cost signal we have, so fall back
    # to the chars/4 estimate of the prompt we actually sent.
    if tokens_in is None:
        tokens_in = estimate_tokens_chars_per_four(prompt)
    if tokens_out is None:
        tokens_out = estimate_tokens_chars_per_four(content)
    return AgentResult(
        ok=True,
        output_text=content,
        backend=name,
        elapsed_seconds=elapsed,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
    )
