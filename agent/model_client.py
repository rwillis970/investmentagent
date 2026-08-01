"""The injected model-client seam for the T4 analysis layer (§3.3, T4 unit,
Commit 4): "the model client is injected; the real one is one
implementation, a fake is the other."

`AnthropicModelClient` is the real implementation, over the same injected
`agent.broker.transport.Transport` abstraction every other network-touching
module in this codebase already uses -- stdlib `urllib` only via
`UrllibTransport`, no dependency added. NO TEST IN THIS CODEBASE'S SUITE
EVER CONSTRUCTS IT WITHOUT A `ScriptedTransport`, and no test calls
`.analyze()` against a real socket -- see tests/test_model_client.py's own
module docstring. This module does not itself make a real API call during
this unit's development; it exists so a real call is POSSIBLE once wired
by whatever eventually calls it (out of scope here -- see agent/analysis.py's
own module docstring for what IS wired in this unit).

CREDENTIALS ARE RESOLVED FRESH ON EVERY CALL, NEVER CACHED ON `self` --
mirrors `agent.broker.alpaca.AlpacaPaperAdapter._headers`'s own documented
discipline (a defect fixed earlier in this project: a cached secret would
not pick up a rotated key without reconstructing the client). `secret_ref`
defaults to `"anthropic_api_key"` -- the one convention this module
introduces for where the Anthropic API key lives in `agent.
secrets_provider.SecretsProvider`; provisioning that entry is out of scope
here, same as every other secret this codebase resolves but does not
provision (see secrets_provider.py's own module docstring).

TOKEN COUNTS ARE THE MODEL'S OWN REPORTED `usage.input_tokens`/
`usage.output_tokens` -- authoritative, not the pre-call heuristic estimate
`agent.analysis.run_analysis` uses to decide whether to call at all. See
that module's own docstring for why the two are deliberately different
numbers used at different times.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from .broker.transport import Transport, UrllibTransport
from .secrets_provider import SecretsProvider

ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_API_VERSION = "2023-06-01"
DEFAULT_SECRET_REF = "anthropic_api_key"


class ModelClientError(Exception):
    pass


@dataclass(frozen=True)
class ModelResponse:
    raw_text: str
    input_tokens: int
    output_tokens: int


class ModelClient(ABC):
    @abstractmethod
    def analyze(self, *, system: str, user: str, max_tokens: int) -> ModelResponse:
        """One model call: a fixed system/instruction string and a user
        message (the delimited data block from `agent.analysis_prompt`).
        Returns the raw response text (expected to be parsed by
        `agent.analysis_output.parse_analysis_output`) plus the model's own
        reported token usage."""


class FakeModelClient(ModelClient):
    """Test double. Never touches a network. Enqueue `ModelResponse`s in
    advance; each `analyze()` call consumes the next one, in order, and is
    recorded in `.calls` -- the same shape as `agent.broker.transport.
    ScriptedTransport`."""

    def __init__(self):
        self._queue: list[ModelResponse] = []
        self.calls: list[dict] = []

    def enqueue(self, response: ModelResponse) -> None:
        self._queue.append(response)

    def analyze(self, *, system: str, user: str, max_tokens: int) -> ModelResponse:
        self.calls.append({"system": system, "user": user, "max_tokens": max_tokens})
        if not self._queue:
            raise AssertionError("FakeModelClient: no more responses queued -- enqueue one before this call")
        return self._queue.pop(0)


class AnthropicModelClient(ModelClient):
    """Real implementation: Anthropic's Messages API over the injected
    `Transport`. See module docstring for credential handling."""

    def __init__(self, *, model_id: str, secrets_provider: SecretsProvider,
                secret_ref: str = DEFAULT_SECRET_REF,
                transport: Transport | None = None, http_timeout_seconds: float = 60.0):
        self._model_id = model_id
        self._secrets = secrets_provider
        self._secret_ref = secret_ref
        self._transport = transport or UrllibTransport()
        self._timeout = http_timeout_seconds

    def _headers(self) -> dict[str, str]:
        # Resolved fresh on every call -- never cached on self. See module
        # docstring's CREDENTIALS section.
        return {
            "x-api-key": self._secrets.resolve(self._secret_ref),
            "anthropic-version": ANTHROPIC_API_VERSION,
            "content-type": "application/json",
        }

    def analyze(self, *, system: str, user: str, max_tokens: int) -> ModelResponse:
        body = {
            "model": self._model_id,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }
        status, resp = self._transport.request(
            "POST", ANTHROPIC_API_URL, headers=self._headers(), json_body=body,
            timeout=self._timeout,
        )
        if status >= 400:
            raise ModelClientError(f"Anthropic API call failed: HTTP {status}: {resp}")
        text = resp["content"][0]["text"]
        usage = resp["usage"]
        return ModelResponse(raw_text=text, input_tokens=usage["input_tokens"],
                            output_tokens=usage["output_tokens"])
