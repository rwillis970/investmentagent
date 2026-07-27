"""Secrets provider (§8, Day 2 gap).

§8's local-deployment table specifies OS keychain, separate entries per mode,
behind a provider interface whose migration seam is a managed vault. This
tests the provider that resolves `agent.accounts.BrokerCredentials`'
`secret_ref` references into actual secret values.

Three properties matter more than anything else here, and each gets its own
section of tests below:
  1. A PAPER-mode provider cannot resolve a live-mode entry -- not "is
     forbidden to," but has no code path that could even form the query.
  2. No secret value ever appears in a raised exception's message, args, or
     chained context -- a secret leaking into a stack trace is the failure
     mode this whole module exists to prevent.
  3. A missing credential is a hard error (`SecretNotFoundError`), never a
     silently-returned empty string.

Tests must never touch the real keychain: `KeychainSecretsProvider`'s only
external call (`subprocess.run` invoking macOS's `security` CLI) is always
mocked here.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from agent.secrets_provider import (InMemorySecretsProvider,
                                    KeychainSecretsProvider,
                                    SecretNotFoundError, SecretsProvider,
                                    _service_name)


# -- 1. mode isolation is structural -----------------------------------------

def test_a_paper_provider_and_a_live_provider_are_independent_even_with_the_same_ref():
    paper = InMemorySecretsProvider(mode="PAPER")
    live = InMemorySecretsProvider(mode="PRODUCTION_ACTIVE")
    paper.put("alpaca-key", "paper-secret-value")
    live.put("alpaca-key", "live-secret-value")

    assert paper.resolve("alpaca-key") == "paper-secret-value"
    assert live.resolve("alpaca-key") == "live-secret-value"


def test_a_paper_provider_cannot_resolve_an_entry_only_ever_put_to_the_live_provider():
    """Not a permission check -- the two providers are simply different
    objects with different backing stores. There is no method on
    InMemorySecretsProvider that takes a mode argument at call time; mode is
    fixed at construction."""
    paper = InMemorySecretsProvider(mode="PAPER")
    live = InMemorySecretsProvider(mode="PRODUCTION_ACTIVE")
    live.put("alpaca-key", "live-secret-value")

    with pytest.raises(SecretNotFoundError):
        paper.resolve("alpaca-key")


def test_resolve_takes_no_mode_argument_mode_is_fixed_at_construction():
    """Structural guard against a future call site accidentally passing a
    mode override at the call site -- resolve()'s signature has no such
    parameter to pass."""
    import inspect
    sig = inspect.signature(SecretsProvider.resolve)
    assert list(sig.parameters) == ["self", "secret_ref"]


def test_keychain_provider_namespaces_the_security_call_by_mode():
    """The real implementation's isolation mechanism: the keychain "service"
    name passed to `security` is namespaced by mode, so a provider bound to
    PAPER can never even construct a query against PRODUCTION_ACTIVE's
    entries."""
    assert _service_name("PAPER") != _service_name("PRODUCTION_ACTIVE")

    paper = KeychainSecretsProvider(mode="PAPER")
    with patch("agent.secrets_provider.subprocess.run") as mock_run:
        mock_run.return_value.stdout = "resolved-value\n"
        mock_run.return_value.returncode = 0
        paper.resolve("alpaca-key")

    args = mock_run.call_args[0][0]
    assert _service_name("PAPER") in args
    assert _service_name("PRODUCTION_ACTIVE") not in args


# -- 2. no secret value ever leaks into an exception ------------------------

def test_missing_secret_error_message_names_the_reference_never_a_value():
    provider = InMemorySecretsProvider(mode="PAPER", entries={"other-key": "unrelated-secret"})
    with pytest.raises(SecretNotFoundError) as exc_info:
        provider.resolve("missing-key")
    message = str(exc_info.value)
    assert "missing-key" in message
    assert "PAPER" in message
    # The exception must not be able to leak any value this provider holds,
    # not just the one that was actually requested.
    assert "unrelated-secret" not in message


def test_missing_secret_error_args_and_repr_never_contain_other_stored_values():
    """str() is not the only way an exception's content ends up in a log --
    repr(), .args, and a bare traceback print all go through different
    machinery. Check all of them."""
    provider = InMemorySecretsProvider(
        mode="PAPER",
        entries={"a": "secret-a-value", "b": "secret-b-value"},
    )
    with pytest.raises(SecretNotFoundError) as exc_info:
        provider.resolve("missing-key")
    exc = exc_info.value
    for surface in (str(exc), repr(exc), str(exc.args), repr(vars(exc))):
        assert "secret-a-value" not in surface
        assert "secret-b-value" not in surface


def test_keychain_lookup_failure_does_not_leak_subprocess_internals():
    """A CalledProcessError's default __str__ includes its `cmd` and can
    include captured stderr -- neither may propagate into what this module
    raises. resolve() must convert failures into a clean SecretNotFoundError
    with no chained cause."""
    import subprocess

    provider = KeychainSecretsProvider(mode="PAPER")
    fake_error = subprocess.CalledProcessError(
        returncode=44, cmd=["security", "find-generic-password", "-w"],
        output="", stderr="some keychain internal detail",
    )
    with patch("agent.secrets_provider.subprocess.run", side_effect=fake_error):
        with pytest.raises(SecretNotFoundError) as exc_info:
            provider.resolve("alpaca-key")

    exc = exc_info.value
    assert "some keychain internal detail" not in str(exc)
    assert exc.__cause__ is None
    assert exc.__suppress_context__ is True


def test_keychain_binary_missing_is_also_a_clean_secret_not_found_error():
    provider = KeychainSecretsProvider(mode="PAPER")
    with patch("agent.secrets_provider.subprocess.run", side_effect=FileNotFoundError()):
        with pytest.raises(SecretNotFoundError):
            provider.resolve("alpaca-key")


# -- 3. missing is a hard error, never a silent empty string -----------------

def test_in_memory_provider_missing_key_raises_not_returns_empty_string():
    provider = InMemorySecretsProvider(mode="PAPER")
    with pytest.raises(SecretNotFoundError):
        provider.resolve("nope")


def test_keychain_provider_empty_stdout_is_treated_as_missing_not_a_valid_empty_secret():
    """`security` exiting 0 with empty stdout is indistinguishable from a
    genuinely empty stored value -- and an empty string is not a usable
    credential either way, so both collapse to the same hard error rather
    than a silent empty-string secret."""
    provider = KeychainSecretsProvider(mode="PAPER")
    with patch("agent.secrets_provider.subprocess.run") as mock_run:
        mock_run.return_value.stdout = ""
        mock_run.return_value.returncode = 0
        with pytest.raises(SecretNotFoundError):
            provider.resolve("alpaca-key")


# -- interface shape ----------------------------------------------------------

def test_both_implementations_satisfy_the_same_interface():
    assert issubclass(KeychainSecretsProvider, SecretsProvider)
    assert issubclass(InMemorySecretsProvider, SecretsProvider)


def test_provider_is_constructed_with_a_mode_and_exposes_it_read_only():
    provider = InMemorySecretsProvider(mode="PAPER")
    assert provider.mode == "PAPER"
    with pytest.raises(AttributeError):
        provider.mode = "PRODUCTION_ACTIVE"


def test_resolved_value_round_trips_exactly():
    provider = InMemorySecretsProvider(mode="PAPER")
    provider.put("k", "exact-value-123")
    assert provider.resolve("k") == "exact-value-123"
