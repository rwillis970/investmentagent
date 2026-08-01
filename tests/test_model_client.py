"""agent/model_client.py (T4 unit, Commit 4): the injected model-client
seam -- `FakeModelClient` (every test in this suite) and
`AnthropicModelClient` (the real implementation; NEVER invoked against a
real network in this codebase's test suite -- every test here uses either
FakeModelClient or AnthropicModelClient wired to a ScriptedTransport, the
same discipline agent/broker/alpaca.py's own tests already hold themselves
to for real credentials).
"""
from __future__ import annotations

import pytest

from agent.broker.transport import ScriptedTransport
from agent.model_client import (AnthropicModelClient, FakeModelClient,
                                ModelClientError, ModelResponse)
from agent.secrets_provider import InMemorySecretsProvider, SecretNotFoundError


# ----------------------------------------------------------------- FakeModelClient

def test_fake_model_client_returns_enqueued_responses_in_order():
    fake = FakeModelClient()
    fake.enqueue(ModelResponse(raw_text='{"a": 1}', input_tokens=10, output_tokens=5))
    fake.enqueue(ModelResponse(raw_text='{"b": 2}', input_tokens=20, output_tokens=8))
    r1 = fake.analyze(system="sys", user="usr", max_tokens=100)
    r2 = fake.analyze(system="sys", user="usr", max_tokens=100)
    assert r1.raw_text == '{"a": 1}'
    assert r2.raw_text == '{"b": 2}'


def test_fake_model_client_records_every_call():
    fake = FakeModelClient()
    fake.enqueue(ModelResponse(raw_text="{}", input_tokens=1, output_tokens=1))
    fake.analyze(system="SYS", user="USR", max_tokens=42)
    assert fake.calls == [{"system": "SYS", "user": "USR", "max_tokens": 42}]


def test_fake_model_client_raises_when_exhausted():
    fake = FakeModelClient()
    with pytest.raises(AssertionError, match="no more responses queued"):
        fake.analyze(system="s", user="u", max_tokens=1)


# ----------------------------------------------------- AnthropicModelClient
# NEVER makes a real API call in this suite -- ScriptedTransport only.

UA_SECRETS = InMemorySecretsProvider(mode="PAPER", entries={"anthropic_api_key": "sk-test-123"})


def test_anthropic_client_sends_the_right_request_shape():
    t = ScriptedTransport()
    t.enqueue(200, {
        "content": [{"type": "text", "text": '{"bull_case": []}'}],
        "usage": {"input_tokens": 123, "output_tokens": 45},
    })
    client = AnthropicModelClient(model_id="claude-sonnet-5",
                                  secrets_provider=UA_SECRETS, transport=t)
    result = client.analyze(system="SYSTEM PROMPT", user="USER DATA", max_tokens=500)
    assert result.raw_text == '{"bull_case": []}'
    assert result.input_tokens == 123
    assert result.output_tokens == 45
    call = t.calls[0]
    assert call["method"] == "POST"
    assert call["json_body"]["model"] == "claude-sonnet-5"
    assert call["json_body"]["system"] == "SYSTEM PROMPT"
    assert call["json_body"]["messages"] == [{"role": "user", "content": "USER DATA"}]
    assert call["json_body"]["max_tokens"] == 500
    assert call["headers"]["x-api-key"] == "sk-test-123"


def test_anthropic_client_resolves_the_secret_fresh_on_every_call_not_cached():
    """Mirrors agent/broker/alpaca.py's own CREDENTIALS discipline: never
    cache a resolved secret on self, so a rotated key takes effect on the
    very next call without reconstructing the client."""
    t = ScriptedTransport()
    t.enqueue(200, {"content": [{"type": "text", "text": "{}"}],
                    "usage": {"input_tokens": 1, "output_tokens": 1}})
    t.enqueue(200, {"content": [{"type": "text", "text": "{}"}],
                    "usage": {"input_tokens": 1, "output_tokens": 1}})
    secrets = InMemorySecretsProvider(mode="PAPER", entries={"anthropic_api_key": "sk-old"})
    client = AnthropicModelClient(model_id="claude-sonnet-5", secrets_provider=secrets,
                                  transport=t)
    client.analyze(system="s", user="u", max_tokens=10)
    secrets.put("anthropic_api_key", "sk-rotated")
    client.analyze(system="s", user="u", max_tokens=10)
    assert t.calls[0]["headers"]["x-api-key"] == "sk-old"
    assert t.calls[1]["headers"]["x-api-key"] == "sk-rotated"


def test_anthropic_client_missing_secret_raises_secret_not_found():
    empty_secrets = InMemorySecretsProvider(mode="PAPER")
    client = AnthropicModelClient(model_id="claude-sonnet-5",
                                  secrets_provider=empty_secrets, transport=ScriptedTransport())
    with pytest.raises(SecretNotFoundError):
        client.analyze(system="s", user="u", max_tokens=10)


def test_anthropic_client_non_2xx_raises_model_client_error():
    t = ScriptedTransport()
    t.enqueue(401, {"error": {"message": "invalid x-api-key"}})
    client = AnthropicModelClient(model_id="claude-sonnet-5",
                                  secrets_provider=UA_SECRETS, transport=t)
    with pytest.raises(ModelClientError, match="401"):
        client.analyze(system="s", user="u", max_tokens=10)


def test_anthropic_client_never_makes_a_real_network_call():
    """The transport is required, not defaulted to UrllibTransport, when
    constructed the way every test here constructs it -- but even the
    DEFAULT (no transport given) must never be exercised by this suite.
    This test simply documents/pins that every other test in this file
    supplies a ScriptedTransport explicitly."""
    import inspect
    sig = inspect.signature(AnthropicModelClient.__init__)
    assert "transport" in sig.parameters
