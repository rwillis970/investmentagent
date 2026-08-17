"""agent/broker/selection.py -- the single broker-adapter selection point
(config-driven-broker-selection unit, 2026-08-10). Tests-first: written
before agent/broker/selection.py existed.

Sandbox has no network egress -- every alpaca_paper case here uses
`agent.broker.transport.ScriptedTransport`, never `UrllibTransport`, and
never a real `KeychainSecretsProvider`. Assertions on an alpaca_paper
request check the REQUEST the adapter would make (method/path/headers-
present/body), never a response from a real Alpaca server.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from agent import config as config_module
from agent.accounts import AccountType, BrokerCredentials
from agent.broker.alpaca import AlpacaPaperAdapter
from agent.broker.base import StagingKeyUnset
from agent.broker.selection import BrokerSelectionError, select_broker_adapter
from agent.broker.simulator import SimulatorBroker
from agent.broker.transport import ScriptedTransport
from agent.daytrade import DayTradeGuard
from agent.pipeline import Gatekeeper
from agent.policy import initial_policy
from agent.risk import PortfolioState, RiskPolicy
from agent.secrets_provider import InMemorySecretsProvider
from tests.test_config_fixture import valid_raw_config

ACCT = "acct-a"
NOW = datetime(2026, 7, 20, 15, 0, tzinfo=timezone.utc)


def _cfg(**overrides):
    return config_module.load(valid_raw_config(**overrides))


def _creds(account_id=ACCT):
    return BrokerCredentials(account_id=account_id, key_id="AK123", secret_ref="alpaca-secret")


def _secrets(mode="PAPER", *, put_secret=True):
    p = InMemorySecretsProvider(mode=mode)
    if put_secret:
        p.put("alpaca-secret", "s3cr3t-value")
    return p


# ----------------------------------------------------------- default/membership

def test_default_config_selects_the_simulator():
    """config.example.json (what valid_raw_config() is built from) names no
    broker key at all -- Config.broker's own default ("simulator") is what
    a caller sees, matching this unit's own "no existing invocation changes
    behavior" instruction."""
    adapter = select_broker_adapter(_cfg(), account_id=ACCT, now=NOW)
    assert isinstance(adapter, SimulatorBroker)
    assert adapter.account_id == ACCT


def test_explicit_simulator_selects_the_simulator():
    adapter = select_broker_adapter(_cfg(broker="simulator"), account_id=ACCT, now=NOW)
    assert isinstance(adapter, SimulatorBroker)


def test_explicit_alpaca_paper_selects_the_alpaca_adapter():
    adapter = select_broker_adapter(
        _cfg(broker="alpaca_paper"), account_id=ACCT,
        credentials=_creds(), secrets_provider=_secrets(),
        transport=ScriptedTransport(),
    )
    assert isinstance(adapter, AlpacaPaperAdapter)
    assert adapter.account_id == ACCT


def test_unrecognised_broker_value_raises_and_constructs_nothing():
    """Belt and suspenders alongside agent.config.validate's own membership
    check (a Config can be constructed directly, bypassing load/validate,
    as this very test does) -- never falls back to the simulator silently."""
    bad_cfg = config_module.Config(broker="not_a_real_broker")

    def _boom_simulator(**kw):
        raise AssertionError("must not construct any adapter for an unrecognised broker value")

    def _boom_alpaca(**kw):
        raise AssertionError("must not construct any adapter for an unrecognised broker value")

    with pytest.raises(BrokerSelectionError, match="not_a_real_broker"):
        select_broker_adapter(
            bad_cfg, account_id=ACCT, credentials=_creds(), secrets_provider=_secrets(),
            simulator_cls=_boom_simulator, alpaca_adapter_cls=_boom_alpaca,
        )


# ----------------------------------------------------------------- credentials

def test_alpaca_paper_without_credentials_raises_and_constructs_nothing():
    def _boom(**kw):
        raise AssertionError("must not construct an adapter with no credentials")

    with pytest.raises(BrokerSelectionError, match="credentials"):
        select_broker_adapter(
            _cfg(broker="alpaca_paper"), account_id=ACCT, credentials=None,
            secrets_provider=_secrets(), alpaca_adapter_cls=_boom,
        )


def test_alpaca_paper_without_secrets_provider_raises_and_constructs_nothing():
    def _boom(**kw):
        raise AssertionError("must not construct an adapter with no secrets_provider")

    with pytest.raises(BrokerSelectionError, match="secrets_provider"):
        select_broker_adapter(
            _cfg(broker="alpaca_paper"), account_id=ACCT, credentials=_creds(),
            secrets_provider=None, alpaca_adapter_cls=_boom,
        )


def test_missing_credential_raises_naming_the_secret_ref_and_setup_doc_and_constructs_nothing():
    """The exact requirement: 'raises with a message naming which keychain
    entry is missing and pointing at docs/credentials-setup.md' -- and does
    NOT construct an adapter (proven, not assumed, via a factory that raises
    if called at all)."""
    def _boom(**kw):
        raise AssertionError("must not construct an adapter when the credential is missing")

    empty_secrets = _secrets(put_secret=False)   # bound to PAPER, but no entry for alpaca-secret
    with pytest.raises(BrokerSelectionError) as exc_info:
        select_broker_adapter(
            _cfg(broker="alpaca_paper"), account_id=ACCT, credentials=_creds(),
            secrets_provider=empty_secrets, alpaca_adapter_cls=_boom,
        )
    message = str(exc_info.value)
    assert "alpaca-secret" in message   # names the missing keychain entry
    assert "docs/credentials-setup.md" in message


# --------------------------------------------------- capability_policy / staging_key

def _gatekeeper(signing_key):
    return Gatekeeper(
        account_id=ACCT, account_type=AccountType.TAXABLE,
        capability_policy=initial_policy(),
        risk_policy=RiskPolicy("t", max_position_pct=50.0, max_sector_pct=100.0,
                               min_settled_cash_pct_of_nlv=0.0, min_absolute_settled_cash=0.0),
        day_trade_guard=DayTradeGuard(account_id=ACCT, max_per_5_sessions=3),
        signing_key=signing_key,
    )


def _staged_order(gk):
    portfolio = PortfolioState(account_id=ACCT, nlv=10000.0, settled_cash=10000.0)
    return gk.stage(client_order_id="c1", symbol="SPY", side="BUY", order_type="LIMIT",
                    time_in_force="DAY", portfolio=portfolio, now=NOW, posture="CASH",
                    qty=1.0, price=100.0, limit_price=100.0)


def test_both_adapter_types_construct_without_a_staging_key_the_same_way():
    """SimulatorBroker's own constructor already documents this (None
    staging_key -> submit()/cancel() refuse everything, fail safe). Verified
    here that select_broker_adapter's alpaca_paper path has the SAME
    posture: construction succeeds with no staging_key given, exactly like
    simulator -- the refusal is not at construction time for either type."""
    sim = select_broker_adapter(_cfg(broker="simulator"), account_id=ACCT, now=NOW)
    assert sim._staging_key is None

    alp = select_broker_adapter(
        _cfg(broker="alpaca_paper"), account_id=ACCT, credentials=_creds(),
        secrets_provider=_secrets(), transport=ScriptedTransport(),
    )
    assert alp._staging_key is None


def test_alpaca_paper_with_no_staging_key_refuses_submit_exactly_like_the_simulator():
    """The fail-safe this unit was asked to verify is not weakened:
    constructing WITHOUT a staging key does not itself refuse anything (see
    the test above) -- submit() is where the refusal actually happens, via
    BrokerAdapter._verify_staged_or_raise, inherited unmodified by every
    subclass (agent/broker/base.py). Mirrors tests/test_broker_alpaca.py's
    own test_submit_without_staging_key_refuses (same shape, same
    Gatekeeper/StagedOrder construction), except the adapter here is built
    through select_broker_adapter, not AlpacaPaperAdapter directly."""
    transport = ScriptedTransport()
    gk = _gatekeeper(signing_key=b"k" * 32)
    staged = _staged_order(gk)

    for cfg_broker in ("simulator", "alpaca_paper"):
        kwargs = dict(account_id=ACCT, capability_policy=initial_policy())
        if cfg_broker == "simulator":
            kwargs["now"] = NOW
        else:
            kwargs.update(credentials=_creds(), secrets_provider=_secrets(),
                          transport=transport)
        adapter = select_broker_adapter(_cfg(broker=cfg_broker), **kwargs)
        with pytest.raises(StagingKeyUnset):
            adapter.submit(staged)
    assert transport.calls == []   # alpaca branch never reached the network either


# ------------------------------------------------------------------- transport seam

def test_alpaca_paper_uses_the_injected_transport_never_a_real_one():
    """The whole point of the seam: this asserts on the REQUEST the adapter
    WOULD make (method, path, headers present -- never their values, body),
    not on any response from Alpaca. No socket is ever opened -- ScriptedTransport
    raises if a call is made with nothing queued."""
    transport = ScriptedTransport()
    transport.enqueue(200, dict(cash="500.00", equity="500.00", buying_power="500.00",
                                multiplier="1", pattern_day_trader=False, daytrade_count=0))
    adapter = select_broker_adapter(
        _cfg(broker="alpaca_paper"), account_id=ACCT, credentials=_creds(),
        secrets_provider=_secrets(), transport=transport,
    )
    adapter.account()

    assert len(transport.calls) == 1
    call = transport.calls[0]
    assert call["method"] == "GET"
    assert call["path"] == "https://paper-api.alpaca.markets/v2/account"
    assert "APCA-API-KEY-ID" in call["headers"]
    assert "APCA-API-SECRET-KEY" in call["headers"]
    # never assert the header VALUES -- see this unit's own instruction


def test_simulator_never_touches_any_transport():
    """A simulator selection must not even accept/require a transport --
    proves the two branches are genuinely independent, not one adapter
    quietly wrapping the other."""
    adapter = select_broker_adapter(_cfg(broker="simulator"), account_id=ACCT, now=NOW)
    assert isinstance(adapter, SimulatorBroker)
    assert not hasattr(adapter, "_transport")


# ------------------------------------------------------- same type, same config

def test_both_call_sites_get_the_same_adapter_type_for_the_same_config():
    """Stands in for 'both scripts/run_agent.py and scripts/run_dashboard.py
    call it' -- two independent calls, shaped like two different call sites
    (one with now= like run_dashboard.py's _build_broker_state, one with
    capability_policy= like run_agent.py's --submit-approved handler), same
    cfg, must resolve to the same adapter type."""
    cfg = _cfg(broker="simulator")
    from_dashboard_shaped_call = select_broker_adapter(cfg, account_id=ACCT, now=NOW)
    from_agent_shaped_call = select_broker_adapter(
        cfg, account_id=ACCT, capability_policy=initial_policy(), now=NOW,
    )
    assert type(from_dashboard_shaped_call) is type(from_agent_shaped_call)


# --------------------------------------------------- capability_policy forwarding

# ------------------------------------ expected_broker_account_id (pin) threading
# (broker-account-uuid-pin-threading follow-up, 2026-08-17)

def test_expected_broker_account_id_reaches_the_alpaca_adapter_only():
    """The pin is forwarded to AlpacaPaperAdapter's own `expected_broker_
    account_id` constructor kwarg -- proven by reading the adapter's own
    attribute, not just by absence of an error."""
    alp = select_broker_adapter(
        _cfg(broker="alpaca_paper"), account_id=ACCT, credentials=_creds(),
        secrets_provider=_secrets(), transport=ScriptedTransport(),
        expected_broker_account_id="pinned-uuid-1",
    )
    assert alp._expected_broker_account_id == "pinned-uuid-1"


def test_expected_broker_account_id_is_never_forwarded_to_the_simulator():
    """Structural guarantee, not just convention: SimulatorBroker.__init__
    has no `expected_broker_account_id` parameter at all -- passing it
    would raise TypeError. A factory that captures its kwargs proves
    select_broker_adapter's simulator branch never even tries."""
    captured = {}

    def _capturing_simulator(**kwargs):
        captured.update(kwargs)
        return SimulatorBroker(**kwargs)

    adapter = select_broker_adapter(
        _cfg(broker="simulator"), account_id=ACCT, now=NOW,
        expected_broker_account_id="pinned-uuid-1",
        simulator_cls=_capturing_simulator,
    )
    assert isinstance(adapter, SimulatorBroker)
    assert "expected_broker_account_id" not in captured

    # Belt and suspenders: the REAL SimulatorBroker class itself would
    # refuse this kwarg -- confirms the guarantee is structural, not just
    # this function choosing not to pass it.
    with pytest.raises(TypeError):
        SimulatorBroker(account_id=ACCT, expected_broker_account_id="x")


def test_no_pin_configured_preserves_existing_alpaca_construction_default():
    """expected_broker_account_id's own default (None, unset by this
    call) must reach AlpacaPaperAdapter identically to before this
    follow-up existed -- unpinned deployments see no behavior change."""
    alp = select_broker_adapter(
        _cfg(broker="alpaca_paper"), account_id=ACCT, credentials=_creds(),
        secrets_provider=_secrets(), transport=ScriptedTransport(),
    )
    assert alp._expected_broker_account_id is None


def test_capability_policy_is_forwarded_identically_to_either_adapter_type():
    policy = initial_policy()
    sim = select_broker_adapter(_cfg(broker="simulator"), account_id=ACCT, now=NOW,
                                capability_policy=policy)
    assert sim.capability_policy is policy

    alp = select_broker_adapter(
        _cfg(broker="alpaca_paper"), account_id=ACCT, credentials=_creds(),
        secrets_provider=_secrets(), transport=ScriptedTransport(),
        capability_policy=policy,
    )
    assert alp.capability_policy is policy
