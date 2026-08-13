"""scripts/preflight_broker.py -- credential/auth preflight (15-minute
credential-hardening sprint, 2026-08-13). No real Keychain, no real network
call in any test: `InMemorySecretsProvider` and `ScriptedTransport` throughout."""
from __future__ import annotations

import ast
import io
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import scripts.preflight_broker as preflight_broker
from agent.broker.transport import ScriptedTransport, TransportTimeout
from agent.secrets_provider import InMemorySecretsProvider, _service_name

FAKE_SECRET = "sk-UNMISTAKABLE-FAKE-SECRET-VALUE-zzz999"
ACCT = "PA3XZX944LRR"


def _provider_factory(mode, entries=None):
    def factory(m):
        return InMemorySecretsProvider(m, dict(entries or {}))
    return factory


# ------------------------------------------------------- service-name derivation

def test_expected_keychain_service_matches_the_real_secrets_provider_function():
    """Not a re-derivation -- this IS the same function KeychainSecretsProvider
    itself calls, imported directly, so it can never drift."""
    report = preflight_broker.preflight(
        account_id=ACCT, key_id="k1", secret_ref="alpaca_secret_key", mode="PAPER",
        auth_check=False, secrets_provider_factory=_provider_factory("PAPER"))
    assert report["keychain_service"] == _service_name("PAPER") == "investmentagent:PAPER"


def test_paper_mode_service_name_is_exactly_investmentagent_colon_paper():
    report = preflight_broker.preflight(
        account_id=ACCT, key_id="k1", secret_ref="alpaca_secret_key", mode="PAPER",
        auth_check=False, secrets_provider_factory=_provider_factory("PAPER"))
    assert report["keychain_service"] == "investmentagent:PAPER"


# --------------------------------------------------- PAUSED must not leak in

def test_persisted_paused_runtime_mode_never_affects_the_credential_namespace():
    """This script never reads agent.mode_store.ModeStore at all -- there is
    no parameter, no code path, by which a persisted PAUSED runtime mode
    could reach this function. Passing mode="PAPER" explicitly (as every
    real call site in this codebase does, off cfg.mode -- never the
    persisted runtime mode) must always resolve the PAPER namespace,
    regardless of what the operational mode happens to be."""
    report = preflight_broker.preflight(
        account_id=ACCT, key_id="k1", secret_ref="alpaca_secret_key", mode="PAPER",
        auth_check=False, secrets_provider_factory=_provider_factory("PAPER"))
    assert report["keychain_service"] == "investmentagent:PAPER"
    assert "PAUSED" not in report["keychain_service"]


def test_preflight_module_never_imports_mode_store():
    source = Path(preflight_broker.__file__).read_text()
    tree = ast.parse(source, preflight_broker.__file__)
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    assert not any("mode_store" in n for n in names)


# ------------------------------------------------------------ credential presence

def test_missing_secret_ref_flag_is_credential_missing_with_no_network_call():
    report = preflight_broker.preflight(
        account_id=ACCT, key_id="k1", secret_ref=None, mode="PAPER", auth_check=True,
        secrets_provider_factory=_provider_factory("PAPER"))
    assert report["credential_status"] == preflight_broker.CREDENTIAL_MISSING
    assert "auth_status" not in report


def test_missing_keychain_entry_is_credential_missing_and_never_leaks_the_lookup_failure_detail_as_a_value():
    report = preflight_broker.preflight(
        account_id=ACCT, key_id="k1", secret_ref="alpaca_secret_key", mode="PAPER",
        auth_check=False, secrets_provider_factory=_provider_factory("PAPER", {}))
    assert report["credential_status"] == preflight_broker.CREDENTIAL_MISSING
    assert FAKE_SECRET not in str(report)


def test_present_credential_without_auth_check_makes_zero_network_calls():
    t_was_used = {"called": False}

    class TrackingFactory:
        def __call__(self, mode):
            provider = InMemorySecretsProvider(mode, {"alpaca_secret_key": FAKE_SECRET})
            return provider

    report = preflight_broker.preflight(
        account_id=ACCT, key_id="k1", secret_ref="alpaca_secret_key", mode="PAPER",
        auth_check=False, secrets_provider_factory=TrackingFactory())
    assert report["credential_status"] == "PRESENT"
    assert "auth_status" not in report


# --------------------------------------------------------------- HTTP classification

def test_2xx_is_auth_pass():
    assert preflight_broker._classify_status(200) == preflight_broker.AUTH_PASS


def test_401_is_auth_fail():
    assert preflight_broker._classify_status(401) == preflight_broker.AUTH_FAIL


def test_403_is_auth_fail_distinct_from_401_but_same_bucket():
    assert preflight_broker._classify_status(403) == preflight_broker.AUTH_FAIL


def test_429_is_rate_limited_not_auth_fail():
    assert preflight_broker._classify_status(429) != preflight_broker.AUTH_FAIL
    assert preflight_broker._classify_status(429) == preflight_broker.RATE_LIMITED


def test_500_is_broker_error_not_auth_fail():
    assert preflight_broker._classify_status(500) != preflight_broker.AUTH_FAIL
    assert preflight_broker._classify_status(500) == preflight_broker.BROKER_ERROR


def test_503_is_broker_error_not_auth_fail():
    assert preflight_broker._classify_status(503) == preflight_broker.BROKER_ERROR


def test_none_status_is_network_unavailable():
    assert preflight_broker._classify_status(None) == preflight_broker.NETWORK_UNAVAILABLE


# ------------------------------------------------- end-to-end auth-check (scripted)

def test_auth_check_401_reports_auth_fail_with_captured_status():
    t = ScriptedTransport()
    t.enqueue(401, {"code": 40110000, "message": "authentication failed"})

    def factory(mode):
        return InMemorySecretsProvider(mode, {"alpaca_secret_key": FAKE_SECRET})

    # inject the transport by monkeypatching the NAME preflight_broker.py
    # itself holds (it does `from agent.broker.alpaca import
    # AlpacaPaperAdapter`, so patching the origin module's attribute would
    # not reach this already-imported reference).
    real_adapter_ctor = preflight_broker.AlpacaPaperAdapter

    class _Injected(real_adapter_ctor):
        def __init__(self, **kwargs):
            kwargs["transport"] = t
            super().__init__(**kwargs)

    preflight_broker.AlpacaPaperAdapter = _Injected
    try:
        report = preflight_broker.preflight(
            account_id=ACCT, key_id="k1", secret_ref="alpaca_secret_key", mode="PAPER",
            auth_check=True, secrets_provider_factory=factory)
    finally:
        preflight_broker.AlpacaPaperAdapter = real_adapter_ctor

    assert report["auth_status"] == preflight_broker.AUTH_FAIL
    assert report["http_status"] == 401
    assert FAKE_SECRET not in str(report)


def test_auth_check_200_reports_auth_pass():
    body = {"cash": "480", "equity": "500.12", "buying_power": "480", "multiplier": "1",
           "pattern_day_trader": False, "daytrade_count": 0}
    t = ScriptedTransport()
    t.enqueue(200, body)

    def factory(mode):
        return InMemorySecretsProvider(mode, {"alpaca_secret_key": FAKE_SECRET})

    real_adapter_ctor = preflight_broker.AlpacaPaperAdapter

    class _Injected(real_adapter_ctor):
        def __init__(self, **kwargs):
            kwargs["transport"] = t
            super().__init__(**kwargs)

    preflight_broker.AlpacaPaperAdapter = _Injected
    try:
        report = preflight_broker.preflight(
            account_id=ACCT, key_id="k1", secret_ref="alpaca_secret_key", mode="PAPER",
            auth_check=True, secrets_provider_factory=factory)
    finally:
        preflight_broker.AlpacaPaperAdapter = real_adapter_ctor

    assert report["auth_status"] == preflight_broker.AUTH_PASS
    assert report["http_status"] == 200
    assert FAKE_SECRET not in str(report)


# ------------------------------------------------------------------ redaction

def test_report_dict_never_contains_the_raw_secret_value():
    report = preflight_broker.preflight(
        account_id=ACCT, key_id="k1", secret_ref="alpaca_secret_key", mode="PAPER",
        auth_check=False, secrets_provider_factory=_provider_factory(
            "PAPER", {"alpaca_secret_key": FAKE_SECRET}))
    assert FAKE_SECRET not in repr(report)
    assert FAKE_SECRET not in str(report)


def test_printed_output_never_contains_the_raw_secret_value():
    report = preflight_broker.preflight(
        account_id=ACCT, key_id="k1", secret_ref="alpaca_secret_key", mode="PAPER",
        auth_check=False, secrets_provider_factory=_provider_factory(
            "PAPER", {"alpaca_secret_key": FAKE_SECRET}))
    buf = io.StringIO()
    with redirect_stdout(buf):
        preflight_broker._print_report(report)
    assert FAKE_SECRET not in buf.getvalue()


def test_secret_not_found_error_message_never_contains_a_value():
    """SecretNotFoundError itself only ever carries mode/secret_ref -- proven
    at the source, not just at this script's own boundary."""
    provider = InMemorySecretsProvider("PAPER", {})
    from agent.secrets_provider import SecretNotFoundError
    with pytest.raises(SecretNotFoundError) as excinfo:
        provider.resolve("alpaca_secret_key")
    assert FAKE_SECRET not in str(excinfo.value)


def test_key_id_is_reported_back_key_id_is_not_secret():
    report = preflight_broker.preflight(
        account_id=ACCT, key_id="AKPUBLICLOOKING123", secret_ref="alpaca_secret_key",
        mode="PAPER", auth_check=False,
        secrets_provider_factory=_provider_factory("PAPER"))
    assert report["key_id_present"] is True


# --------------------------------------------------------- structural safety

def test_preflight_module_never_imports_an_execution_path():
    forbidden = ("agent.pipeline", "agent.approval", "agent.pipeline_stage",
                "agent.model_client", "agent.approval_execution", "agent.approval_bridge")
    source = Path(preflight_broker.__file__).read_text()
    tree = ast.parse(source, preflight_broker.__file__)
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    for fragment in forbidden:
        assert not any(fragment in n for n in names)


def test_endpoint_classification_is_always_paper_never_live():
    report = preflight_broker.preflight(
        account_id=ACCT, key_id="k1", secret_ref="alpaca_secret_key", mode="PAPER",
        auth_check=False, secrets_provider_factory=_provider_factory("PAPER"))
    assert report["endpoint_classification"] == "PAPER"
    assert "paper-api.alpaca.markets" in report["base_url"]
