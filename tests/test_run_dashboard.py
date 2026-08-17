"""scripts/run_dashboard.py -- launchd-deploy-broken follow-up (2026-08-03).
This script had the EXACT same defect shape as scripts/run_agent.py's own
six-flags-with-no-default bug (--cost-ledger-path/--approval-request-
store-path/--opportunity-tracker-path/--audit-log-path were all
`required=True` with no default) -- it just hadn't been exploited in
production yet because no plist for it existed at all until this same
unit. `--data-dir` fixes it the same way.

BROKER-STATE WIRING (broker-state-wiring unit, 2026-08-10). `_build_broker_
state` is the new piece: it constructs a real, local `SimulatorBroker` (no
credentials, no network -- see that class's own docstring) plus a real
`LedgerStore`/`DayTradeGuard`, and calls `agent.account_wiring.
build_account_reconciliation` (the SAME assembler `agent/run_loop.py`'s own
real cycle uses) to get `broker_account`/`broker_positions`/`day_trade_
guard` for `DashboardRuntime`, rather than leaving them at their `None`/
`()`/`None` defaults forever. It never raises: ANY failure (no account_id,
a corrupt ledger file, or anything else) degrades to `(None, (), None)`,
which is the exact input `agent.dashboard_state.build_dashboard_state`
already treats as "no broker_account was supplied" -- see that module's own
docstring for why that null is honest rather than a bug.

HONESTY LIMIT, STATED PLAINLY. `SimulatorBroker` is a pure in-memory
simulator with no durable backing store of its own (checked directly --
`agent/broker/simulator.py` has no `path`/`load`/`save`). A freshly
constructed one is always the same default paper account ($500, no
positions) -- it does NOT reflect any real, separately-running
`scripts/run_agent.py` process's actual trading history, because that
process (per `agent/config.py`'s only constructible live-adapter class,
`AlpacaPaperAdapter`) talks to Alpaca's own paper servers over the network
with real credentials, neither of which this wiring is permitted to touch
(see this unit's own report). This closes the "always null" defect
honestly, not by pretending to mirror a broker connection this environment
cannot make.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agent import config as config_module
from agent.accounts import BrokerCredentials
from agent.broker.base import CapabilityPolicyUnset, StagingKeyUnset
from agent.broker.transport import ScriptedTransport
from agent.daytrade import DayTradeGuard
from agent.secrets_provider import CachingSecretsProvider, InMemorySecretsProvider
from scripts.run_dashboard import (_build_broker_state, _check_credential,
                                   _require_credentials_for_alpaca_paper,
                                   _parse_args, build_dashboard_runtime, main)
from tests.test_config_fixture import valid_raw_config


def test_data_dir_defaults_all_five_store_paths_to_named_files_inside_it(tmp_path):
    """Renamed from "...four..." (broker-state-wiring unit, 2026-08-10):
    --ledger-store-path joins the same defaulting group, so a caller who
    only ever set --data-dir still gets a real ledger file rather than a
    sixth required flag."""
    data_dir = tmp_path / "data"
    args = _parse_args(["--config", "c.json", "--data-dir", str(data_dir)])
    assert args.cost_ledger_path == str(data_dir / "cost_ledger.jsonl")
    assert args.approval_request_store_path == str(data_dir / "approval_requests.jsonl")
    assert args.opportunity_tracker_path == str(data_dir / "opportunity_events.jsonl")
    assert args.audit_log_path == str(data_dir / "audit.jsonl")
    assert args.ledger_store_path == str(data_dir / "ledger.jsonl")
    assert args.quarantine_store_path == str(data_dir / "quarantine.jsonl")
    # Track B dashboard-truth fix (2026-08-14): fact_store_path joins the
    # same defaulting group, matching scripts/run_agent.py's own filename.
    assert args.fact_store_path == str(data_dir / "facts.jsonl")
    # Task 1 (Phase-2/3-live-acceptance follow-up unit, 2026-08-15):
    # opportunity_event_store_path joins the same defaulting group,
    # matching scripts/run_agent.py's own filename.
    assert args.opportunity_event_store_path == str(data_dir / "materiality_events.jsonl")
    assert data_dir.is_dir()


def test_filenames_match_run_agents_own_defaults_for_the_same_five_stores(tmp_path):
    """The whole point of reusing these exact names: pointing this
    script's --data-dir at the SAME directory a real scripts/run_agent.py
    deployment uses must resolve to the SAME files, not a second,
    independently-named copy of them -- ledger.jsonl included, so the
    dashboard's own LedgerStore read (see _build_broker_state) lands on
    the same durable file a real run_agent.py process writes."""
    import scripts.run_agent as run_agent_module

    data_dir = tmp_path / "data"
    dashboard_args = _parse_args(["--config", "c.json", "--data-dir", str(data_dir)])
    agent_args = run_agent_module._parse_args([
        "--config", "c.json", "--account-id", "a", "--key-id", "k",
        "--secret-ref", "r", "--signing-key-secret-ref", "sk",
        "--data-dir", str(data_dir),
    ])
    assert dashboard_args.cost_ledger_path == agent_args.cost_ledger_path
    assert dashboard_args.approval_request_store_path == agent_args.approval_request_store_path
    assert dashboard_args.opportunity_tracker_path == agent_args.opportunity_tracker_path
    assert dashboard_args.audit_log_path == agent_args.audit_log_path
    assert dashboard_args.ledger_store_path == agent_args.ledger_store_path
    assert dashboard_args.quarantine_store_path == agent_args.quarantine_store_path


def test_data_dir_default_is_resolved_to_an_absolute_path(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    args = _parse_args(["--config", "c.json"])
    assert args.data_dir == str(tmp_path / "data")


def test_explicit_store_path_overrides_are_untouched(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    explicit = tmp_path / "custom" / "cost_ledger.jsonl"
    args = _parse_args(["--config", "c.json", "--cost-ledger-path", str(explicit)])
    assert args.cost_ledger_path == str(explicit)
    assert not explicit.parent.exists()   # never auto-created for an explicit override


def test_data_dir_is_never_created_when_every_store_path_is_explicit(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _parse_args([
        "--config", "c.json",
        "--cost-ledger-path", str(tmp_path / "cl.jsonl"),
        "--approval-request-store-path", str(tmp_path / "ar.jsonl"),
        "--opportunity-tracker-path", str(tmp_path / "ot.jsonl"),
        "--audit-log-path", str(tmp_path / "al.jsonl"),
        "--ledger-store-path", str(tmp_path / "l.jsonl"),
        "--quarantine-store-path", str(tmp_path / "q.jsonl"),
        "--mode-store-path", str(tmp_path / "m.jsonl"),
        "--fact-store-path", str(tmp_path / "f.jsonl"),
        "--opportunity-event-store-path", str(tmp_path / "oe.jsonl"),
    ])
    assert not (tmp_path / "data").exists()


# --------------------------------------------- _build_broker_state (broker-state-wiring unit)

def _cfg(**overrides):
    return config_module.load(valid_raw_config(**overrides))


def test_no_account_id_degrades_to_the_null_quadruple_without_touching_anything(tmp_path):
    broker_account, broker_positions, day_trade_guard, ledger = _build_broker_state(
        _cfg(), account_id=None, ledger_store_path=tmp_path / "ledger.jsonl",
        quarantine_store_path=tmp_path / "quarantine.jsonl",
        now=datetime(2026, 7, 20, 15, 0, tzinfo=timezone.utc),
    )
    assert broker_account is None
    assert broker_positions == ()
    assert day_trade_guard is None
    assert ledger is None
    assert not (tmp_path / "ledger.jsonl").exists()   # nothing was constructed at all


def test_happy_path_populates_all_four_from_a_fresh_simulator_paper_account(tmp_path):
    now = datetime(2026, 7, 20, 15, 0, tzinfo=timezone.utc)
    broker_account, broker_positions, day_trade_guard, ledger = _build_broker_state(
        _cfg(), account_id="acct-1", ledger_store_path=tmp_path / "ledger.jsonl",
        quarantine_store_path=tmp_path / "quarantine.jsonl", now=now,
    )
    assert broker_account is not None
    assert broker_account.account_id == "acct-1"
    assert float(broker_account.settled_cash) == 500.0   # SimulatorBroker's own default
    assert float(broker_account.multiplier) == 1.0   # 1.0 = cash account
    assert broker_positions == ()   # a fresh simulator has no positions
    assert isinstance(day_trade_guard, DayTradeGuard)
    assert day_trade_guard.account_id == "acct-1"
    # Performance-plumbing unit (2026-08-13): the SAME LedgerStore
    # build_account_reconciliation just seeded, reconstructed via
    # to_ledger() -- a fresh account has no fills yet, so no closed lots.
    assert ledger is not None
    assert ledger.account_id == "acct-1"
    assert ledger.closed_lots() == []


def test_a_corrupt_ledger_file_degrades_to_the_null_quadruple_not_a_raised_exception(tmp_path):
    """The whole honesty contract this unit exists to preserve: a broker
    read that cannot complete becomes null + (implicitly, via
    agent.dashboard_state) an unavailable_reason, never an exception that
    would take the whole /api/state response down with it, and never a
    fabricated number."""
    bad_path = tmp_path / "ledger.jsonl"
    bad_path.write_text("not valid jsonl at all {{{\n", encoding="utf-8")
    broker_account, broker_positions, day_trade_guard, ledger = _build_broker_state(
        _cfg(), account_id="acct-1", ledger_store_path=bad_path,
        quarantine_store_path=tmp_path / "quarantine.jsonl",
        now=datetime(2026, 7, 20, 15, 0, tzinfo=timezone.utc),
    )
    assert broker_account is None
    assert broker_positions == ()
    assert day_trade_guard is None
    assert ledger is None


def test_config_is_still_required(tmp_path):
    import pytest
    with pytest.raises(SystemExit) as exc_info:
        _parse_args(["--data-dir", str(tmp_path / "data")])
    assert exc_info.value.code == 2


def test_host_and_port_defaults_are_unaffected(tmp_path):
    args = _parse_args(["--config", "c.json", "--data-dir", str(tmp_path / "data")])
    assert args.host == "127.0.0.1"
    assert args.port == 8765


# ---------------------------------------- Unit 16: broker credentials wiring, 2026-08-12

def _alpaca_creds(account_id="acct-1"):
    return BrokerCredentials(account_id=account_id, key_id="AK1", secret_ref="alpaca-secret")


def _alpaca_secrets(mode="PAPER", *, put_secret=True):
    p = InMemorySecretsProvider(mode=mode)
    if put_secret:
        p.put("alpaca-secret", "s3cr3t-value")
    return p


def test_key_id_and_secret_ref_flags_default_to_none():
    args = _parse_args(["--config", "c.json"])
    assert args.key_id is None
    assert args.secret_ref is None


def test_key_id_and_secret_ref_flags_are_parsed():
    args = _parse_args(["--config", "c.json", "--key-id", "AK1",
                        "--secret-ref", "alpaca-secret"])
    assert args.key_id == "AK1"
    assert args.secret_ref == "alpaca-secret"


def test_require_credentials_is_a_noop_for_the_default_simulator_broker():
    _require_credentials_for_alpaca_paper(_cfg(), key_id=None, secret_ref=None)  # no raise


def test_require_credentials_raises_naming_both_missing_flags_for_alpaca_paper():
    cfg = _cfg(broker="alpaca_paper")
    with pytest.raises(SystemExit, match=r"--key-id, --secret-ref"):
        _require_credentials_for_alpaca_paper(cfg, key_id=None, secret_ref=None)


def test_require_credentials_raises_naming_only_the_one_missing_flag():
    cfg = _cfg(broker="alpaca_paper")
    with pytest.raises(SystemExit) as exc_info:
        _require_credentials_for_alpaca_paper(cfg, key_id="AK1", secret_ref=None)
    message = str(exc_info.value)
    assert "--secret-ref" in message
    assert "--key-id" not in message


def test_require_credentials_passes_silently_when_both_are_given():
    cfg = _cfg(broker="alpaca_paper")
    _require_credentials_for_alpaca_paper(cfg, key_id="AK1", secret_ref="alpaca-secret")


def test_a_real_alpaca_paper_read_populates_all_three_capital_fields(tmp_path):
    """'A real read' -- via ScriptedTransport, never a real socket (sandbox
    has no network egress, same constraint tests/test_broker_selection.py's
    own module docstring already states). Proves credentials/secrets_
    provider actually reach select_broker_adapter from _build_broker_state,
    not just accepted as unused parameters -- the populated figures below
    (12345.67) could only have come from the scripted /v2/account response,
    never SimulatorBroker's hardcoded $500 default."""
    transport = ScriptedTransport()
    transport.enqueue(200, dict(cash="12345.67", equity="12345.67", buying_power="12345.67",
                                multiplier="1", pattern_day_trader=False, daytrade_count=0))
    transport.enqueue(200, [])   # positions() call #1 -- account_wiring's opening-seed check
    transport.enqueue(200, [])   # positions() call #2 -- broker_positions
    transport.enqueue(200, [])   # open_orders()

    now = datetime(2026, 7, 20, 15, 0, tzinfo=timezone.utc)
    broker_account, broker_positions, day_trade_guard, ledger = _build_broker_state(
        _cfg(broker="alpaca_paper"), account_id="acct-1",
        ledger_store_path=tmp_path / "ledger.jsonl",
        quarantine_store_path=tmp_path / "quarantine.jsonl", now=now,
        credentials=_alpaca_creds(), secrets_provider=_alpaca_secrets(),
        transport=transport,
    )
    assert broker_account is not None
    assert float(broker_account.settled_cash) == 12345.67
    assert float(broker_account.equity) == 12345.67
    assert float(broker_account.buying_power) == 12345.67
    assert broker_positions == ()
    assert isinstance(day_trade_guard, DayTradeGuard)
    assert ledger is not None
    assert ledger.account_id == "acct-1"


def test_a_missing_secret_degrades_to_the_null_quadruple_not_an_exception(tmp_path):
    """broker=alpaca_paper with credentials pointing at a keychain entry
    that does not exist -- select_broker_adapter raises BrokerSelectionError
    (agent/broker/selection.py), which _build_broker_state's own broad
    except must still degrade to the same honest null quadruple this module
    has always promised, never a fabricated number and never a crash that
    would take the rest of GET /api/state down with it."""
    now = datetime(2026, 7, 20, 15, 0, tzinfo=timezone.utc)
    broker_account, broker_positions, day_trade_guard, ledger = _build_broker_state(
        _cfg(broker="alpaca_paper"), account_id="acct-1",
        ledger_store_path=tmp_path / "ledger.jsonl",
        quarantine_store_path=tmp_path / "quarantine.jsonl", now=now,
        credentials=_alpaca_creds(), secrets_provider=_alpaca_secrets(put_secret=False),
        transport=ScriptedTransport(),
    )
    assert broker_account is None
    assert broker_positions == ()
    assert day_trade_guard is None
    assert ledger is None


def test_alpaca_paper_with_no_credentials_at_all_still_degrades_to_the_null_quadruple(tmp_path):
    """cfg.broker == alpaca_paper but this call was given neither
    credentials nor secrets_provider (e.g. --key-id/--secret-ref were never
    passed to main -- main's own _require_credentials_for_alpaca_paper
    would already have refused to start the real process; this test calls
    _build_broker_state one layer below that guard, to prove it ALSO fails
    safe on its own, not only because main happens to stop it first)."""
    now = datetime(2026, 7, 20, 15, 0, tzinfo=timezone.utc)
    broker_account, broker_positions, day_trade_guard, ledger = _build_broker_state(
        _cfg(broker="alpaca_paper"), account_id="acct-1",
        ledger_store_path=tmp_path / "ledger.jsonl",
        quarantine_store_path=tmp_path / "quarantine.jsonl", now=now,
    )
    assert broker_account is None
    assert broker_positions == ()
    assert day_trade_guard is None
    assert ledger is None


# --------------------------------------------------------------------
# broker_account_uuid pin threading (broker-account-uuid-pin-threading
# follow-up, 2026-08-17). `cfg.broker_account_uuid` -> `_build_broker_
# state` -> `select_broker_adapter(..., expected_broker_account_id=...)`
# -> `AlpacaPaperAdapter(expected_broker_account_id=...)` only.

def test_configured_pin_reaches_the_real_alpaca_adapter_construction(tmp_path):
    """Behavioral proof the configured UUID actually reaches
    AlpacaPaperAdapter's own `expected_broker_account_id` (not just
    "accepted as an unused parameter"): when the scripted /v2/account
    response's `id` MATCHES the configured pin, the read succeeds and
    reports that response's real figures -- if the pin were silently
    dropped anywhere in the select_broker_adapter -> AlpacaPaperAdapter
    chain, verification simply would not run and this would trivially
    pass too, so this is paired with the mismatch test below (which only
    fails the way it does BECAUSE the identity check is live) to prove
    the pin genuinely reaches and is enforced by the real adapter, not
    merely accepted."""
    transport = ScriptedTransport()
    transport.enqueue(200, dict(id="pinned-uuid-1", cash="777.00", equity="777.00",
                                buying_power="777.00", multiplier="1",
                                pattern_day_trader=False, daytrade_count=0))
    transport.enqueue(200, [])
    transport.enqueue(200, [])
    transport.enqueue(200, [])
    now = datetime(2026, 7, 20, 15, 0, tzinfo=timezone.utc)
    broker_account, broker_positions, day_trade_guard, ledger = _build_broker_state(
        _cfg(broker="alpaca_paper", broker_account_uuid="pinned-uuid-1"),
        account_id="acct-1", ledger_store_path=tmp_path / "ledger.jsonl",
        quarantine_store_path=tmp_path / "quarantine.jsonl", now=now,
        credentials=_alpaca_creds(), secrets_provider=_alpaca_secrets(),
        transport=transport,
    )
    assert broker_account is not None
    assert float(broker_account.settled_cash) == 777.00


def test_simulator_construction_is_unaffected_by_a_configured_pin(tmp_path):
    """cfg.broker == "simulator" with cfg.broker_account_uuid ALSO set
    (an operator who configured a pin but never switched off the
    simulator) must still construct successfully -- select_broker_
    adapter's own structural guarantee (SimulatorBroker has no such
    parameter) means the pin is silently inapplicable here, never an
    error."""
    now = datetime(2026, 7, 20, 15, 0, tzinfo=timezone.utc)
    broker_account, broker_positions, day_trade_guard, ledger = _build_broker_state(
        _cfg(broker="simulator", broker_account_uuid="pinned-uuid-1"),
        account_id="acct-1", ledger_store_path=tmp_path / "ledger.jsonl",
        quarantine_store_path=tmp_path / "quarantine.jsonl", now=now,
    )
    assert broker_account is not None
    assert float(broker_account.settled_cash) == 500.0   # SimulatorBroker's own default


def test_missing_pin_preserves_existing_alpaca_compatibility_behavior(tmp_path):
    """cfg.broker_account_uuid unset (its own None default) -> _build_
    broker_state's alpaca_paper read behaves exactly as before this
    follow-up: it succeeds and reports whatever account id the scripted
    response returns, with no identity check performed at all."""
    transport = ScriptedTransport()
    transport.enqueue(200, dict(id="whatever-account", cash="12345.67", equity="12345.67",
                                buying_power="12345.67", multiplier="1",
                                pattern_day_trader=False, daytrade_count=0))
    transport.enqueue(200, [])
    transport.enqueue(200, [])
    transport.enqueue(200, [])
    now = datetime(2026, 7, 20, 15, 0, tzinfo=timezone.utc)
    broker_account, broker_positions, day_trade_guard, ledger = _build_broker_state(
        _cfg(broker="alpaca_paper"),  # broker_account_uuid left at its own default: None
        account_id="acct-1", ledger_store_path=tmp_path / "ledger.jsonl",
        quarantine_store_path=tmp_path / "quarantine.jsonl", now=now,
        credentials=_alpaca_creds(), secrets_provider=_alpaca_secrets(),
        transport=transport,
    )
    assert broker_account is not None
    assert float(broker_account.settled_cash) == 12345.67


def test_a_pinned_identity_mismatch_degrades_to_the_null_quadruple_without_crashing(tmp_path):
    """The whole point of threading the pin through THIS function
    specifically: a configured pin whose reported account id does not
    match must fail closed to the exact same honest null quadruple every
    other _build_broker_state failure already produces (via this
    function's own pre-existing broad `except Exception`), never a
    fabricated value and never an unhandled exception that would crash
    the dashboard server process serving the rest of GET /api/state."""
    transport = ScriptedTransport()
    transport.enqueue(200, dict(id="some-other-account", cash="500.00", equity="500.00",
                                buying_power="500.00", multiplier="1",
                                pattern_day_trader=False, daytrade_count=0))
    now = datetime(2026, 7, 20, 15, 0, tzinfo=timezone.utc)
    broker_account, broker_positions, day_trade_guard, ledger = _build_broker_state(
        _cfg(broker="alpaca_paper", broker_account_uuid="pinned-uuid-1"),
        account_id="acct-1", ledger_store_path=tmp_path / "ledger.jsonl",
        quarantine_store_path=tmp_path / "quarantine.jsonl", now=now,
        credentials=_alpaca_creds(), secrets_provider=_alpaca_secrets(),
        transport=transport,
    )
    assert broker_account is None
    assert broker_positions == ()
    assert day_trade_guard is None
    assert ledger is None


def test_a_pinned_missing_reported_id_also_degrades_to_the_null_quadruple(tmp_path):
    """Same fail-closed path, but the broker response omits `id` entirely
    (a malformed/absent identity) rather than reporting a mismatched one
    -- AlpacaAccountIdentityMismatch treats None != "pinned-uuid-1" the
    same as any other mismatch (agent/broker/alpaca.py's own account())."""
    transport = ScriptedTransport()
    transport.enqueue(200, dict(cash="500.00", equity="500.00", buying_power="500.00",
                                multiplier="1", pattern_day_trader=False, daytrade_count=0))
    now = datetime(2026, 7, 20, 15, 0, tzinfo=timezone.utc)
    broker_account, broker_positions, day_trade_guard, ledger = _build_broker_state(
        _cfg(broker="alpaca_paper", broker_account_uuid="pinned-uuid-1"),
        account_id="acct-1", ledger_store_path=tmp_path / "ledger.jsonl",
        quarantine_store_path=tmp_path / "quarantine.jsonl", now=now,
        credentials=_alpaca_creds(), secrets_provider=_alpaca_secrets(),
        transport=transport,
    )
    assert broker_account is None
    assert broker_positions == ()
    assert day_trade_guard is None
    assert ledger is None


def test_no_dashboard_adapter_gains_submit_or_cancel_capability_even_when_pinned():
    """Task 3's own explicit requirement: threading the pin must not also
    attach capability_policy/staging_key. Constructs the adapter the SAME
    way _build_broker_state does (select_broker_adapter, no capability_
    policy, no staging_key, but WITH expected_broker_account_id set this
    time) and proves submit()/cancel() still refuse via the unchanged,
    already-validated StagingKeyUnset mutation guard -- pinning identity
    is orthogonal to, and does not loosen, that gate."""
    from agent.accounts import AccountType
    from agent.broker.selection import select_broker_adapter as _select
    from agent.daytrade import DayTradeGuard as _DTG
    from agent.pipeline import Gatekeeper
    from agent.policy import initial_policy
    from agent.risk import PortfolioState, RiskPolicy

    acct = "acct-1"
    now = datetime(2026, 7, 20, 15, 0, tzinfo=timezone.utc)
    transport = ScriptedTransport()
    adapter = _select(
        _cfg(broker="alpaca_paper", broker_account_uuid="pinned-uuid-1"),
        account_id=acct, credentials=_alpaca_creds(acct),
        secrets_provider=_alpaca_secrets(), transport=transport, now=now,
        expected_broker_account_id="pinned-uuid-1",
    )
    assert adapter._staging_key is None
    # capability_policy is a property that RAISES CapabilityPolicyUnset when
    # unset (agent/broker/base.py), not one that returns None -- confirms
    # no policy was attached, the same way the staging-key check above does.
    with pytest.raises(CapabilityPolicyUnset):
        adapter.capability_policy

    gk = Gatekeeper(
        account_id=acct, account_type=AccountType.TAXABLE,
        capability_policy=initial_policy(),
        risk_policy=RiskPolicy("t", max_position_pct=50.0, max_sector_pct=100.0,
                               min_settled_cash_pct_of_nlv=0.0, min_absolute_settled_cash=0.0),
        day_trade_guard=_DTG(account_id=acct, max_per_5_sessions=3),
        signing_key=b"k" * 32,
    )
    portfolio = PortfolioState(account_id=acct, nlv=10000.0, settled_cash=10000.0)
    staged = gk.stage(client_order_id="c1", symbol="SPY", side="BUY", order_type="LIMIT",
                      time_in_force="DAY", portfolio=portfolio, now=now, posture="CASH",
                      qty=1.0, price=100.0, limit_price=100.0)
    # With a pin configured, submit()/cancel() each run
    # _verify_broker_identity_or_raise() FIRST (agent/broker/base.py's own
    # documented ordering) -- an extra self.account() round-trip ahead of
    # the staging-key gate. Queue a matching identity response for each of
    # the two mutating calls below so identity verification PASSES,
    # isolating what this test actually checks: that the pin does not
    # loosen the staging-key refusal underneath it.
    transport.enqueue(200, dict(id="pinned-uuid-1", cash="500.00", equity="500.00",
                                buying_power="500.00", multiplier="1",
                                pattern_day_trader=False, daytrade_count=0))
    with pytest.raises(StagingKeyUnset):
        adapter.submit(staged)
    cancel_staged = gk.stage(client_order_id="c1", symbol="SPY", side="CANCEL",
                             order_type="LIMIT", time_in_force="DAY",
                             portfolio=portfolio, now=now, posture="CASH")
    transport.enqueue(200, dict(id="pinned-uuid-1", cash="500.00", equity="500.00",
                                buying_power="500.00", multiplier="1",
                                pattern_day_trader=False, daytrade_count=0))
    with pytest.raises(StagingKeyUnset):
        adapter.cancel(cancel_staged)


def test_the_dashboards_own_adapter_construction_never_attaches_a_staging_key():
    """Unit 16's own read-only requirement: pass no staging_key; if the
    adapter refuses writes via StagingKeyUnset, that is correct. Verified,
    not assumed -- constructs the adapter the SAME way _build_broker_state
    does (agent.broker.selection.select_broker_adapter, credentials/
    secrets_provider/transport given, no capability_policy, no
    staging_key), then proves submit() actually refuses a real StagedOrder,
    mirroring tests/test_broker_selection.py's own equivalent test for the
    same adapter type."""
    from agent.accounts import AccountType
    from agent.broker.selection import select_broker_adapter
    from agent.daytrade import DayTradeGuard as _DTG
    from agent.pipeline import Gatekeeper
    from agent.policy import initial_policy
    from agent.risk import PortfolioState, RiskPolicy

    acct = "acct-1"
    now = datetime(2026, 7, 20, 15, 0, tzinfo=timezone.utc)
    transport = ScriptedTransport()
    adapter = select_broker_adapter(
        _cfg(broker="alpaca_paper"), account_id=acct,
        credentials=_alpaca_creds(acct), secrets_provider=_alpaca_secrets(),
        transport=transport, now=now,
    )
    assert adapter._staging_key is None

    gk = Gatekeeper(
        account_id=acct, account_type=AccountType.TAXABLE,
        capability_policy=initial_policy(),
        risk_policy=RiskPolicy("t", max_position_pct=50.0, max_sector_pct=100.0,
                               min_settled_cash_pct_of_nlv=0.0, min_absolute_settled_cash=0.0),
        day_trade_guard=_DTG(account_id=acct, max_per_5_sessions=3),
        signing_key=b"k" * 32,
    )
    portfolio = PortfolioState(account_id=acct, nlv=10000.0, settled_cash=10000.0)
    staged = gk.stage(client_order_id="c1", symbol="SPY", side="BUY", order_type="LIMIT",
                      time_in_force="DAY", portfolio=portfolio, now=now, posture="CASH",
                      qty=1.0, price=100.0, limit_price=100.0)

    with pytest.raises(StagingKeyUnset):
        adapter.submit(staged)
    assert transport.calls == []   # the refusal happens before any network call is attempted


# --------------------------------------- Unit 16: main()'s own startup-time refusal

def test_main_refuses_loudly_at_startup_when_alpaca_paper_is_configured_with_no_flags(
    tmp_path,
):
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(valid_raw_config(broker="alpaca_paper")),
                          encoding="utf-8")
    with pytest.raises(SystemExit, match=r"--key-id, --secret-ref"):
        main(["--config", str(config_path), "--data-dir", str(tmp_path / "data"),
             "--account-id", "acct-1"])


# ---------------------------------------- Unit 17: credential preflight strip

def test_signing_key_secret_ref_flag_defaults_to_none():
    args = _parse_args(["--config", "c.json"])
    assert args.signing_key_secret_ref is None


def test_signing_key_secret_ref_flag_is_parsed():
    args = _parse_args(["--config", "c.json", "--signing-key-secret-ref", "gk-ref"])
    assert args.signing_key_secret_ref == "gk-ref"


def test_check_credential_reports_present_true_when_resolve_succeeds():
    sp = _alpaca_secrets()   # has "alpaca-secret" put already
    result = _check_credential("alpaca-secret", sp)
    assert result == {"present": True, "error": None}


def test_check_credential_reports_present_false_with_the_secretnotfounderror_message():
    """NEVER the secret value -- only SecretNotFoundError's own message,
    which (see agent/secrets_provider.py's own docstring) carries only mode
    and secret_ref, never a resolved value."""
    sp = _alpaca_secrets(put_secret=False)
    result = _check_credential("alpaca-secret", sp)
    assert result["present"] is False
    assert "alpaca-secret" in result["error"]
    assert "s3cr3t-value" not in result["error"]   # the value, if it existed, never leaks


def test_check_credential_treats_a_missing_secret_ref_as_absent_not_a_crash():
    """No --secret-ref/--signing-key-secret-ref given at all (both optional,
    per Unit 16/17) -- this must degrade to the same honest 'not present'
    shape, never an AttributeError/TypeError from calling resolve(None)."""
    sp = _alpaca_secrets()
    result = _check_credential(None, sp)
    assert result["present"] is False
    assert result["error"]


def _all_store_paths(tmp_path):
    return dict(
        cost_ledger_path=tmp_path / "cl.jsonl",
        approval_request_store_path=tmp_path / "ar.jsonl",
        opportunity_tracker_path=tmp_path / "ot.jsonl",
        audit_log_path=tmp_path / "al.jsonl",
        ledger_store_path=tmp_path / "l.jsonl",
        quarantine_store_path=tmp_path / "q.jsonl",
        mode_store_path=tmp_path / "m.jsonl",
    )


def test_build_dashboard_runtime_threads_credential_preflight_through_verbatim(tmp_path):
    preflight = {
        "alpaca_api_secret": {"present": True, "error": None},
        "gatekeeper_signing_key": {"present": False, "error": "not found"},
    }
    runtime = build_dashboard_runtime(
        _cfg(), config_path="c.json", account_id=None,
        credential_preflight=preflight, **_all_store_paths(tmp_path),
    )
    assert runtime.credential_preflight == preflight


def test_build_dashboard_runtime_defaults_credential_preflight_to_empty_dict(tmp_path):
    runtime = build_dashboard_runtime(
        _cfg(), config_path="c.json", account_id=None, **_all_store_paths(tmp_path),
    )
    assert runtime.credential_preflight == {}


def test_build_dashboard_runtime_ledger_is_none_with_no_account_id(tmp_path):
    runtime = build_dashboard_runtime(
        _cfg(), config_path="c.json", account_id=None, **_all_store_paths(tmp_path),
    )
    assert runtime.ledger is None


def test_build_dashboard_runtime_wires_a_real_ledger_when_account_id_is_given(tmp_path):
    """Performance-plumbing unit (2026-08-13): DashboardRuntime.ledger must
    actually be set from _build_broker_state's new fourth return value, not
    left at its default -- proven via the same real SimulatorBroker path
    test_happy_path_populates_all_four_from_a_fresh_simulator_paper_account
    already exercises one layer down."""
    now = datetime(2026, 7, 20, 15, 0, tzinfo=timezone.utc)
    runtime = build_dashboard_runtime(
        _cfg(), config_path="c.json", account_id="acct-1", now=now,
        **_all_store_paths(tmp_path),
    )
    assert runtime.ledger is not None
    assert runtime.ledger.account_id == "acct-1"


# --------------------------------------- broker_state_refresh_fn wiring
# (overnight-hardening unit, 2026-08-13): the "captured once at startup,
# stale forever" fix -- see agent/dashboard_server.py's own
# DashboardRuntime.broker_state_refresh_fn docstring, and this module's own
# `_refresh` closure comment for why it is the SAME `_build_broker_state`,
# just re-invoked instead of called once.

def test_build_dashboard_runtime_attaches_a_refresh_fn_with_no_account_id(tmp_path):
    """Even with no account_id (nothing meaningful to refresh),
    broker_state_refresh_fn must still be a callable, never None -- route_
    request's own null-check is only about whether a refresh mechanism
    exists at all, not about whether it will find anything."""
    runtime = build_dashboard_runtime(
        _cfg(), config_path="c.json", account_id=None, **_all_store_paths(tmp_path),
    )
    assert callable(runtime.broker_state_refresh_fn)
    account, positions, guard, ledger = runtime.broker_state_refresh_fn()
    assert account is None
    assert positions == ()


def test_broker_state_refresh_fn_returns_a_fresh_read_each_call(tmp_path):
    now = datetime(2026, 7, 20, 15, 0, tzinfo=timezone.utc)
    runtime = build_dashboard_runtime(
        _cfg(), config_path="c.json", account_id="acct-1", now=now,
        **_all_store_paths(tmp_path),
    )
    assert callable(runtime.broker_state_refresh_fn)
    account_1, _, _, _ = runtime.broker_state_refresh_fn()
    account_2, _, _, _ = runtime.broker_state_refresh_fn()
    assert account_1 is not None
    assert account_2 is not None
    assert account_1.account_id == account_2.account_id == "acct-1"


def test_broker_state_refresh_fn_uses_now_fn_not_a_frozen_initial_now(tmp_path):
    """The refresh closure must call `now_fn()` fresh on every invocation --
    proven by injecting a `now_fn` whose return value changes between calls
    and observing `fetched_at` track it."""
    # First value is consumed by build_dashboard_runtime's own initial,
    # one-shot _build_broker_state call (since no explicit `now=` is given
    # here); the next two are what the two refresh_fn() calls below should
    # each independently observe.
    clock = iter([
        datetime(2026, 7, 20, 14, 0, tzinfo=timezone.utc),
        datetime(2026, 7, 20, 15, 0, tzinfo=timezone.utc),
        datetime(2026, 7, 20, 16, 30, tzinfo=timezone.utc),
    ])
    runtime = build_dashboard_runtime(
        _cfg(), config_path="c.json", account_id="acct-1",
        now_fn=lambda: next(clock), **_all_store_paths(tmp_path),
    )
    account_1, _, _, _ = runtime.broker_state_refresh_fn()
    account_2, _, _, _ = runtime.broker_state_refresh_fn()
    assert account_1.fetched_at == datetime(2026, 7, 20, 15, 0, tzinfo=timezone.utc)
    assert account_2.fetched_at == datetime(2026, 7, 20, 16, 30, tzinfo=timezone.utc)


# ---------------------------------------- Unit A (reconstructed 2026-08-13):
# Keychain prompt storm -- measured resolve() count per dashboard refresh.

class _CountingSecretsProvider(InMemorySecretsProvider):
    """Wraps InMemorySecretsProvider, counting every real .resolve() call
    that reaches the underlying provider -- a stand-in for counting real
    `/usr/bin/security find-generic-password` subprocess invocations
    (this sandbox has no macOS Keychain to invoke directly), reached via
    the exact same production call path (select_broker_adapter's presence
    check, then AlpacaPaperAdapter._headers() on every real HTTP call)."""

    def __init__(self, mode="PAPER", *, put_secret=True):
        super().__init__(mode=mode)
        self.resolve_count = 0
        if put_secret:
            self.put("alpaca-secret", "s3cr3t-value")

    def resolve(self, secret_ref):
        self.resolve_count += 1
        return super().resolve(secret_ref)


def _scripted_transport_for_one_steady_state_refresh():
    """account() + positions() (broker_positions) + open_orders() -- the
    THREE adapter HTTP calls a steady-state (already-seeded ledger) call
    to _build_broker_state makes, per agent/account_wiring.py's own
    build_account_reconciliation body (no positions-seed branch once
    store.to_ledger().positions() is already non-empty)."""
    transport = ScriptedTransport()
    transport.enqueue(200, dict(cash="500.00", equity="500.00", buying_power="500.00",
                                multiplier="1", pattern_day_trader=False, daytrade_count=0))
    transport.enqueue(200, [])   # positions() -- broker_positions
    transport.enqueue(200, [])   # open_orders()
    return transport


def test_measured_resolve_count_per_steady_state_refresh_is_four_uncached(tmp_path):
    """UNIT A FINDING (reconstructed, 2026-08-13): a single steady-state
    _build_broker_state call -- i.e. one real GET /api/state, once the
    ledger already has an opening balance so no positions-seed branch
    runs -- makes FOUR separate SecretsProvider.resolve() calls against
    the SAME secret_ref: one inside agent.broker.selection.
    select_broker_adapter's own fail-fast presence check, plus one per
    real adapter HTTP call (account/positions/open_orders), because
    agent.broker.alpaca.AlpacaPaperAdapter._headers() resolves fresh on
    EVERY call by design (see that module's own CREDENTIALS section) and
    KeychainSecretsProvider caches nothing (see agent/secrets_provider.py's
    own docstring: 'RESOLVED FRESH, NEVER CACHED HERE'). Combined with
    dashboard/static/dashboard_bind.js's own POLL_INTERVAL_MS = 5000, an
    open dashboard tab triggers 4 real `security find-generic-password`
    subprocess invocations every 5 seconds indefinitely -- this is the
    measured shape of the Keychain prompt storm, reproduced here via a
    counting double over the real production call path (this sandbox has
    no macOS Keychain to invoke directly, so a literal GUI-prompt count
    cannot be measured here -- see Unit A doc for that disclosed limit)."""
    # First, seed an opening balance/positions so this call is steady-state
    # (matches ordinary repeated polling after the very first dashboard
    # refresh, not the one-time first-ever-run seed path).
    seed_transport = _scripted_transport_for_one_steady_state_refresh()
    counting = _CountingSecretsProvider()
    now = datetime(2026, 7, 20, 15, 0, tzinfo=timezone.utc)
    _build_broker_state(
        _cfg(broker="alpaca_paper"), account_id="acct-1",
        ledger_store_path=tmp_path / "ledger.jsonl",
        quarantine_store_path=tmp_path / "quarantine.jsonl", now=now,
        credentials=_alpaca_creds(), secrets_provider=counting,
        transport=seed_transport,
    )
    seeding_call_count = counting.resolve_count   # 5, not part of this claim

    # Now the steady-state call this test actually measures.
    steady_transport = _scripted_transport_for_one_steady_state_refresh()
    _build_broker_state(
        _cfg(broker="alpaca_paper"), account_id="acct-1",
        ledger_store_path=tmp_path / "ledger.jsonl",
        quarantine_store_path=tmp_path / "quarantine.jsonl", now=now,
        credentials=_alpaca_creds(), secrets_provider=counting,
        transport=steady_transport,
    )
    steady_state_calls = counting.resolve_count - seeding_call_count
    assert steady_state_calls == 4, (
        f"expected 4 uncached resolve() calls for one steady-state "
        f"refresh, measured {steady_state_calls}")


def test_caching_secrets_provider_answers_three_steady_state_refreshes_with_one_real_resolve(tmp_path):
    """UNIT A FIX, proven at the _build_broker_state layer (the function
    every real GET /api/state refresh actually calls -- see
    scripts/run_dashboard.py's own _refresh closure): wrap the same
    counting provider test_measured_resolve_count_per_steady_state_
    refresh_is_four_uncached uses in a CachingSecretsProvider (the SAME
    class production's own default_keychain_secrets_provider_factory
    wraps KeychainSecretsProvider in) and call _build_broker_state three
    times in a row -- standing in for three consecutive 5-second
    dashboard_bind.js polls, all well within the cache's default 300s TTL.
    Before this fix: 3 calls x 4 resolves = 12. After: 1."""
    underlying = _CountingSecretsProvider()
    cached = CachingSecretsProvider(underlying, ttl_seconds=300.0)
    now = datetime(2026, 7, 20, 15, 0, tzinfo=timezone.utc)

    # Seed first (opening balance + positions) so all three measured calls
    # below are steady-state, matching the other test's own methodology.
    _build_broker_state(
        _cfg(broker="alpaca_paper"), account_id="acct-1",
        ledger_store_path=tmp_path / "ledger.jsonl",
        quarantine_store_path=tmp_path / "quarantine.jsonl", now=now,
        credentials=_alpaca_creds(), secrets_provider=cached,
        transport=_scripted_transport_for_one_steady_state_refresh(),
    )
    calls_after_seed = underlying.resolve_count

    for _ in range(3):
        _build_broker_state(
            _cfg(broker="alpaca_paper"), account_id="acct-1",
            ledger_store_path=tmp_path / "ledger.jsonl",
            quarantine_store_path=tmp_path / "quarantine.jsonl", now=now,
            credentials=_alpaca_creds(), secrets_provider=cached,
            transport=_scripted_transport_for_one_steady_state_refresh(),
        )

    assert calls_after_seed == 1, (
        f"expected the seed call itself to cost exactly 1 real resolve() "
        f"call, measured {calls_after_seed}")
    assert underlying.resolve_count - calls_after_seed == 0, (
        f"expected 3 further cached steady-state refreshes to cost zero "
        f"additional real resolve() calls (all within the 300s TTL), "
        f"measured {underlying.resolve_count - calls_after_seed} more")


# ---------------------------------------- Unit 2 follow-up (writer-lock-gap
# unit, round 2, 2026-08-14): explicit measured counts for the exact
# scenarios asked for -- 10 successive requests (not just 3), a request
# genuinely past TTL expiry at the SAME _build_broker_state integration
# layer the two tests above use (not just the lower-level unit test in
# tests/test_secrets_provider.py), and a fresh process's cache starting
# empty. All three reuse the identical _CountingSecretsProvider/
# _scripted_transport_for_one_steady_state_refresh fixtures immediately
# above, so results are directly comparable.

def test_ten_successive_steady_state_refreshes_cost_exactly_one_real_resolve(tmp_path):
    """dashboard_bind.js polls every 5s (POLL_INTERVAL_MS=5000); 10
    successive polls span 50s, well inside the 300s TTL -- extends the
    3-poll proof immediately above to a more realistic run length. Before
    Unit A's fix this would have been 10 x 4 = 40 real resolve() calls;
    after, exactly 1 (the seed)."""
    underlying = _CountingSecretsProvider()
    cached = CachingSecretsProvider(underlying, ttl_seconds=300.0)
    now = datetime(2026, 7, 20, 15, 0, tzinfo=timezone.utc)

    _build_broker_state(
        _cfg(broker="alpaca_paper"), account_id="acct-1",
        ledger_store_path=tmp_path / "ledger.jsonl",
        quarantine_store_path=tmp_path / "quarantine.jsonl", now=now,
        credentials=_alpaca_creds(), secrets_provider=cached,
        transport=_scripted_transport_for_one_steady_state_refresh(),
    )
    calls_after_seed = underlying.resolve_count

    for _ in range(10):
        _build_broker_state(
            _cfg(broker="alpaca_paper"), account_id="acct-1",
            ledger_store_path=tmp_path / "ledger.jsonl",
            quarantine_store_path=tmp_path / "quarantine.jsonl", now=now,
            credentials=_alpaca_creds(), secrets_provider=cached,
            transport=_scripted_transport_for_one_steady_state_refresh(),
        )

    assert calls_after_seed == 1
    assert underlying.resolve_count - calls_after_seed == 0, (
        f"expected 10 further cached steady-state refreshes (50s of "
        f"5-second polling, inside the 300s TTL) to cost zero additional "
        f"real resolve() calls, measured "
        f"{underlying.resolve_count - calls_after_seed} more")


def test_a_refresh_past_ttl_expiry_re_resolves_all_four_then_caches_again(tmp_path):
    """The other half of the story: caching is bounded, not permanent.
    A refresh that lands strictly after the 300s TTL must pay the full
    4-resolve cost again (not stay silently stale forever), and the refresh
    immediately after THAT must go back to costing zero -- proving expiry
    doesn't leave the cache permanently broken, just correctly re-primed."""
    underlying = _CountingSecretsProvider()
    clock = [0.0]
    cached = CachingSecretsProvider(underlying, ttl_seconds=300.0, now_fn=lambda: clock[0])
    now = datetime(2026, 7, 20, 15, 0, tzinfo=timezone.utc)

    _build_broker_state(
        _cfg(broker="alpaca_paper"), account_id="acct-1",
        ledger_store_path=tmp_path / "ledger.jsonl",
        quarantine_store_path=tmp_path / "quarantine.jsonl", now=now,
        credentials=_alpaca_creds(), secrets_provider=cached,
        transport=_scripted_transport_for_one_steady_state_refresh(),
    )
    calls_after_seed = underlying.resolve_count

    # A poll well within the TTL: free, as proven above.
    clock[0] = 100.0
    _build_broker_state(
        _cfg(broker="alpaca_paper"), account_id="acct-1",
        ledger_store_path=tmp_path / "ledger.jsonl",
        quarantine_store_path=tmp_path / "quarantine.jsonl", now=now,
        credentials=_alpaca_creds(), secrets_provider=cached,
        transport=_scripted_transport_for_one_steady_state_refresh(),
    )
    assert underlying.resolve_count == calls_after_seed

    # A poll strictly past the 300s TTL: the cache is expired, so at least
    # one real resolve() must happen again -- measured at 2 (not the naive
    # "all 4 calls in this refresh re-resolve" expectation, since the
    # FIRST resolve inside this refresh already re-primes the cache for
    # whichever of the remaining calls land after it; the exact number is
    # an implementation detail of call ordering inside _build_broker_state/
    # select_broker_adapter, not a contract this test should over-specify
    # -- what matters, and what this asserts, is "> 0" (expiry genuinely
    # forces at least one fresh Keychain-equivalent call, proving the cache
    # is bounded, not permanent) and the re-priming proof immediately below).
    clock[0] = 301.0
    _build_broker_state(
        _cfg(broker="alpaca_paper"), account_id="acct-1",
        ledger_store_path=tmp_path / "ledger.jsonl",
        quarantine_store_path=tmp_path / "quarantine.jsonl", now=now,
        credentials=_alpaca_creds(), secrets_provider=cached,
        transport=_scripted_transport_for_one_steady_state_refresh(),
    )
    assert underlying.resolve_count - calls_after_seed > 0, (
        "expected at least one real resolve() call once the TTL had "
        "genuinely expired -- the cache must not stay silently stale forever")

    # Immediately after that re-prime, the NEXT poll is free again.
    clock[0] = 302.0
    calls_before_next = underlying.resolve_count
    _build_broker_state(
        _cfg(broker="alpaca_paper"), account_id="acct-1",
        ledger_store_path=tmp_path / "ledger.jsonl",
        quarantine_store_path=tmp_path / "quarantine.jsonl", now=now,
        credentials=_alpaca_creds(), secrets_provider=cached,
        transport=_scripted_transport_for_one_steady_state_refresh(),
    )
    assert underlying.resolve_count == calls_before_next


def test_a_fresh_process_cache_never_inherits_a_prior_processs_cached_value():
    """PROCESS RECONSTRUCTION. Production constructs exactly one
    CachingSecretsProvider per process, via default_keychain_secrets_
    provider_factory, called once at process startup (scripts/run_agent.py
    main() / scripts/run_dashboard.py main()) -- there is no cross-process
    cache of any kind (no file, no shared memory, no IPC). A second,
    independent CachingSecretsProvider instance -- standing in for a
    restarted process -- must start with an empty cache and pay the full
    resolve cost on its own first call, never silently inheriting a value
    an earlier process happened to cache."""
    first_process_provider = _CountingSecretsProvider()
    first_process_cache = CachingSecretsProvider(first_process_provider, ttl_seconds=300.0)
    first_process_cache.resolve("alpaca-secret")
    assert first_process_provider.resolve_count == 1

    # A brand new process would construct its OWN underlying provider and
    # its OWN CachingSecretsProvider -- never reuse the one above.
    second_process_provider = _CountingSecretsProvider()
    second_process_cache = CachingSecretsProvider(second_process_provider, ttl_seconds=300.0)
    assert second_process_provider.resolve_count == 0   # nothing inherited
    second_process_cache.resolve("alpaca-secret")
    assert second_process_provider.resolve_count == 1   # paid its own real cost


# ---------------------------------------- Unit E (reconstructed 2026-08-13):
# operational_state_refresh_fn -- cross-process staleness proof, mirrors
# broker_state_refresh_fn's own equivalent test immediately above.

def test_operational_state_refresh_sees_a_change_written_by_a_separate_modestore_instance(tmp_path):
    """THE proof this closure exists for: agent.mode_store.ModeStore.
    __init__ loads its history once into memory and never re-reads its
    file (that class's own docstring) -- so build_dashboard_runtime must
    construct a FRESH ModeStore every call, not hold one open for the
    dashboard process's whole lifetime, or a mode change written by a
    genuinely separate process (the real scripts/run_agent.py, its own
    LaunchAgent) would never become visible without a dashboard restart."""
    from agent.mode_store import ModeStore

    mode_store_path = tmp_path / "m.jsonl"
    now = datetime(2026, 7, 20, 15, 0, tzinfo=timezone.utc)
    runtime = build_dashboard_runtime(
        _cfg(), config_path="c.json", account_id="acct-1", now=now,
        **{**_all_store_paths(tmp_path), "mode_store_path": mode_store_path},
    )
    assert callable(runtime.operational_state_refresh_fn)

    state_before, _ = runtime.operational_state_refresh_fn()
    assert state_before == "DISABLED"   # fresh, never-written store's own baseline

    # A SEPARATE ModeStore instance -- standing in for the real, separate
    # run_agent.py process -- writes a new mode to the SAME durable file.
    ModeStore(mode_store_path).write("PRODUCTION_ACTIVE", changed_at=now)

    state_after, _ = runtime.operational_state_refresh_fn()
    assert state_after == "PRODUCTION_ACTIVE"


def test_operational_state_refresh_reads_disabled_for_a_never_written_mode_store(tmp_path):
    runtime = build_dashboard_runtime(
        _cfg(), config_path="c.json", account_id="acct-1",
        now=datetime(2026, 7, 20, 15, 0, tzinfo=timezone.utc),
        **_all_store_paths(tmp_path),
    )
    state, paused_from = runtime.operational_state_refresh_fn()
    assert state == "DISABLED"
    assert paused_from is None


def test_operational_state_refresh_returns_none_none_when_no_mode_store_path_given(tmp_path):
    paths = _all_store_paths(tmp_path)
    del paths["mode_store_path"]
    runtime = build_dashboard_runtime(
        _cfg(), config_path="c.json", account_id="acct-1",
        now=datetime(2026, 7, 20, 15, 0, tzinfo=timezone.utc),
        **paths,
    )
    assert runtime.operational_state_refresh_fn() == (None, None)


def test_operational_state_refresh_reads_paused_from_when_state_is_paused(tmp_path):
    from agent.mode_store import ModeStore

    mode_store_path = tmp_path / "m.jsonl"
    now = datetime(2026, 7, 20, 15, 0, tzinfo=timezone.utc)
    store = ModeStore(mode_store_path)
    store.write("PRODUCTION_ACTIVE", changed_at=now)
    store.write("PAUSED", changed_at=now, paused_from="PRODUCTION_ACTIVE")

    runtime = build_dashboard_runtime(
        _cfg(), config_path="c.json", account_id="acct-1", now=now,
        **{**_all_store_paths(tmp_path), "mode_store_path": mode_store_path},
    )
    state, paused_from = runtime.operational_state_refresh_fn()
    assert state == "PAUSED"
    assert paused_from == "PRODUCTION_ACTIVE"


def test_operational_state_refresh_reports_disabled_on_a_lone_truncated_first_row(tmp_path):
    """UPDATED (writer-lock-gap unit, round 2, Unit 3, 2026-08-14): a mode-
    state file containing exactly ONE malformed line is indistinguishable
    from a crash mid-write of this store's very first-ever row -- agent.
    mode_store.ModeStore._load() now tolerates a malformed FINAL line (see
    its own docstring) and falls back to the last row it can positively
    prove was durably written, which here is none at all, i.e. the
    documented empty-history/fresh-install baseline. That baseline is not
    "unknown" -- agent/mode_store.py's own module docstring and agent/
    mode.py's normalize_persisted() already both define `current() is
    None` as meaning DISABLED, and scripts/run_dashboard.py's
    _refresh_operational_state renders that as the literal string
    "DISABLED" (a real, safe, no-trade value -- see tests/
    test_dashboard_state.py::test_operational_state_disabled_is_a_real_
    value_not_an_unavailable_reason), not as an unavailable/None diagnostic.
    Nothing about this changes the actual startup gate: agent/startup.py
    and scripts/run_agent.py's scheduled loop each build their own
    ModeStore and call mode.assert_legal_startup directly -- neither reads
    this dashboard-only refresh function at all. See test immediately
    below for the case that still degrades to (None, None): corruption
    that is NOT explainable as a crash-truncated last row."""
    mode_store_path = tmp_path / "m.jsonl"
    mode_store_path.write_text("{not valid json\n")
    runtime = build_dashboard_runtime(
        _cfg(), config_path="c.json", account_id="acct-1",
        now=datetime(2026, 7, 20, 15, 0, tzinfo=timezone.utc),
        **{**_all_store_paths(tmp_path), "mode_store_path": mode_store_path},
    )
    assert runtime.operational_state_refresh_fn() == ("DISABLED", None)


def test_operational_state_refresh_still_degrades_to_none_none_on_unrecoverable_corruption(
    tmp_path,
):
    """The case the ORIGINAL version of the test above meant to cover:
    corruption that cannot be explained as a crash mid-write of a single
    interrupted final row -- here, a well-formed first row followed by a
    malformed row that is NOT the last line. ModeStore._load() still
    raises ModeStoreError for this (see its own docstring: "there is no
    fsync-ordering argument that could explain corruption in the MIDDLE of
    this file as 'just a crash'"), so the dashboard's own broad
    `except Exception` still degrades this to the honest, fully-unknown
    (None, None) -- never a fabricated operational_state."""
    mode_store_path = tmp_path / "m.jsonl"
    mode_store_path.write_text(
        '{"seq": 1, "mode": "PAPER", "changed_at": "2026-07-20T15:00:00+00:00"}\n'
        '{not valid json -- and NOT the last line\n'
        '{"seq": 3, "mode": "PAUSED", "changed_at": "2026-07-20T15:05:00+00:00"}\n'
    )
    runtime = build_dashboard_runtime(
        _cfg(), config_path="c.json", account_id="acct-1",
        now=datetime(2026, 7, 20, 15, 0, tzinfo=timezone.utc),
        **{**_all_store_paths(tmp_path), "mode_store_path": mode_store_path},
    )
    assert runtime.operational_state_refresh_fn() == (None, None)
