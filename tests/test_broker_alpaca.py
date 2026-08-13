"""Alpaca paper adapter (§1.2, §11 Day 10 -- moved ahead of the collectors:
one API serves both paper and live, so the adapter is built once; only the
paper half is built and enabled here, per this unit's explicit constraint).

No test here ever makes a network call -- every test injects a
`ScriptedTransport` (agent/broker/transport.py) and asserts against what was
sent/enqueued. `BrokerAdapter`'s existing contract (the `_submit_impl`/
`_cancel_impl` staging-key gate, `__init_subclass__`'s public-method
discipline, idempotency on client_order_id) all apply unchanged -- this
file is not re-testing those, only what `AlpacaPaperAdapter` adds.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from agent.accounts import BrokerCredentials, CrossAccountError
from agent.approval import ApprovalService, order_fingerprint
from agent.broker.alpaca import (STATUS_MAP, AlpacaError, AlpacaPaperAdapter,
                                 AmbiguousOrderState, UnsupportedOrderShape)
from agent.broker.base import (TERMINAL_ORDER_STATUSES, AccountSnapshot, BrokerOrder,
                               Execution, Position, StagingKeyUnset)
from agent.broker.transport import ScriptedTransport, TransportError, TransportTimeout
from agent.pipeline import Gatekeeper
from agent.risk import PortfolioState
from agent.secrets_provider import InMemorySecretsProvider
from agent.policy import initial_policy

ACCT = "acct-a"


def secrets(mode="PAPER", key="s3cr3t-value"):
    p = InMemorySecretsProvider(mode=mode)
    p.put("alpaca-secret", key)
    return p


def credentials(account_id=ACCT):
    return BrokerCredentials(account_id=account_id, key_id="AK123", secret_ref="alpaca-secret")


def adapter(transport=None, *, secrets_provider=None, staging_key=None,
           policy=None, max_retries=2, account_id=ACCT):
    return AlpacaPaperAdapter(
        account_id=account_id, credentials=credentials(account_id),
        secrets_provider=secrets_provider or secrets(),
        capability_policy=policy or initial_policy(),
        staging_key=staging_key,
        transport=transport or ScriptedTransport(),
        http_timeout_seconds=1.0, http_max_retries=max_retries,
    )


def account_json(**over):
    base = dict(cash="500.00", equity="500.00", buying_power="500.00",
               multiplier="1", pattern_day_trader=False, daytrade_count=0)
    base.update(over)
    return base


def order_json(**over):
    base = dict(id="alpaca-order-1", client_order_id="c1", symbol="SPY",
               side="buy", qty="1", filled_qty="0", type="limit",
               order_type="limit", time_in_force="day", limit_price="500.00",
               filled_avg_price=None, status="new",
               submitted_at="2026-07-20T13:00:00Z", filled_at=None)
    base.update(over)
    return base


# ------------------------------------------------------------- construction

def test_requires_credentials():
    with pytest.raises(AlpacaError):
        AlpacaPaperAdapter(account_id=ACCT, credentials=None, secrets_provider=secrets(),
                          transport=ScriptedTransport())


def test_rejects_a_secrets_provider_not_bound_to_paper():
    """Structural isolation, mirroring SecretsProvider: this is checked at
    construction, before a single request is made."""
    with pytest.raises(AlpacaError, match="PAPER"):
        adapter(secrets_provider=secrets(mode="PRODUCTION_ACTIVE"))


def test_cross_account_credentials_are_rejected_same_as_any_adapter():
    mismatched = BrokerCredentials(account_id="acct-b", key_id="k", secret_ref="alpaca-secret")
    with pytest.raises(CrossAccountError):
        AlpacaPaperAdapter(account_id=ACCT, credentials=mismatched, secrets_provider=secrets(),
                          transport=ScriptedTransport())


def test_is_live_is_false_and_name_is_set():
    a = adapter()
    assert a.is_live is False
    assert a.name == "alpaca_paper"


def test_base_url_is_the_paper_endpoint():
    assert AlpacaPaperAdapter.BASE_URL == "https://paper-api.alpaca.markets"


# ---------------------------------------------------------------- account()

def test_account_maps_the_documented_fields():
    t = ScriptedTransport()
    t.enqueue(200, account_json(cash="500.00", equity="512.34", buying_power="500.00",
                                multiplier="1", pattern_day_trader=True, daytrade_count=2))
    snap = adapter(t).account()
    assert isinstance(snap, AccountSnapshot)
    assert snap.account_id == ACCT
    # Exact Decimal comparison, not a float literal: 512.34 has no exact
    # binary float representation, so `Decimal('512.34') == 512.34` is False
    # (float(512.34) is actually 512.33999999999996...) -- precisely the
    # representational noise the 2026-07-28 Decimal migration exists to
    # eliminate (see agent/money.py). Comparing against the same decimal
    # string Alpaca sent is the exact, correct check here.
    assert snap.equity == Decimal("512.34")
    assert snap.cash == 500.0
    assert snap.buying_power == 500.0
    assert snap.multiplier == 1.0
    assert snap.pattern_day_trader is True
    assert snap.day_trade_count == 2


def test_account_maps_absent_pdt_fields_to_none_not_false_or_zero():
    """FINDING (§13 probe, 2026-07-27 -- scripts/fixtures/account.json): a
    real, brand-new Alpaca paper cash account OMITS `pattern_day_trader` and
    `daytrade_count` entirely -- not `false`/`0`. Silently defaulting an
    absent safety-relevant field to a concrete value is exactly what
    Appendix E's fail-safe-to-NO-TRADE forbids: it invents data. Both must
    come back as `None` (unknown), not `False`/`0`."""
    t = ScriptedTransport()
    body = account_json()
    del body["pattern_day_trader"]
    del body["daytrade_count"]
    t.enqueue(200, body)
    snap = adapter(t).account()
    assert snap.pattern_day_trader is None
    assert snap.day_trade_count is None


def test_account_still_maps_real_pdt_fields_when_present():
    """The unknown-vs-known distinction must not regress the ordinary case:
    when Alpaca DOES report these fields, they still come through as the
    real values, same as before this change."""
    t = ScriptedTransport()
    t.enqueue(200, account_json(pattern_day_trader=True, daytrade_count=2))
    snap = adapter(t).account()
    assert snap.pattern_day_trader is True
    assert snap.day_trade_count == 2


def test_account_maps_a_reported_false_and_zero_as_known_not_unknown():
    """The other edge: Alpaca explicitly reporting `false`/`0` (a genuinely
    known, non-PDT, zero-day-trades account) must NOT be conflated with the
    field being absent -- both are legitimate, distinct states."""
    t = ScriptedTransport()
    t.enqueue(200, account_json(pattern_day_trader=False, daytrade_count=0))
    snap = adapter(t).account()
    assert snap.pattern_day_trader is False
    assert snap.day_trade_count == 0


def test_account_settled_cash_is_mapped_from_cash_and_is_approximate():
    """Alpaca's /v2/account has no settled/unsettled split (confirmed
    against alpaca-py's TradeAccount model -- no such field exists).
    settled_cash is mapped from `cash` as the closest available figure;
    unsettled_cash is always 0.0. This is a documented approximation, not
    an exact mapping -- see agent/broker/alpaca.py's module docstring."""
    t = ScriptedTransport()
    t.enqueue(200, account_json(cash="123.45"))
    snap = adapter(t).account()
    assert snap.settled_cash == Decimal("123.45")
    assert snap.unsettled_cash == 0.0


def test_account_request_targets_the_paper_base_url_and_get_endpoint():
    t = ScriptedTransport()
    t.enqueue(200, account_json())
    adapter(t).account()
    call = t.calls[0]
    assert call["method"] == "GET"
    assert call["path"] == "https://paper-api.alpaca.markets/v2/account"


def test_account_request_carries_credentials_headers_and_no_secret_leaks_client_side():
    t = ScriptedTransport()
    t.enqueue(200, account_json())
    adapter(t, secrets_provider=secrets(key="the-real-secret")).account()
    headers = t.calls[0]["headers"]
    assert headers["APCA-API-KEY-ID"] == "AK123"
    assert headers["APCA-API-SECRET-KEY"] == "the-real-secret"


def test_account_read_retries_on_timeout_then_succeeds():
    t = ScriptedTransport()
    t.enqueue_error(TransportTimeout("slow"))
    t.enqueue_error(TransportTimeout("slow again"))
    t.enqueue(200, account_json())
    snap = adapter(t, max_retries=2).account()
    assert snap.cash == 500.0
    assert len(t.calls) == 3   # 1 original + 2 retries


def test_account_read_exhausts_retries_and_raises():
    t = ScriptedTransport()
    t.enqueue_error(TransportTimeout("slow"))
    t.enqueue_error(TransportTimeout("slow"))
    t.enqueue_error(TransportTimeout("slow"))
    with pytest.raises(TransportTimeout):
        adapter(t, max_retries=2).account()
    assert len(t.calls) == 3   # 1 original + 2 retries, no more


def test_account_read_with_zero_retries_makes_exactly_one_attempt():
    t = ScriptedTransport()
    t.enqueue_error(TransportTimeout("slow"))
    with pytest.raises(TransportTimeout):
        adapter(t, max_retries=0).account()
    assert len(t.calls) == 1


# -------------------------------------------------------------- positions()

def test_positions_maps_long_position_fields():
    t = ScriptedTransport()
    t.enqueue(200, [{"symbol": "SPY", "qty": "2", "side": "long",
                    "avg_entry_price": "100.00", "market_value": "210.00"}])
    positions = adapter(t).positions()
    assert positions == [Position(account_id=ACCT, symbol="SPY", qty=2.0,
                                  avg_price=100.0, market_value=210.0)]


def test_positions_short_side_is_represented_as_negative_qty():
    """Shorting is DISABLED at the capability layer (Appendix E) for this
    pilot regardless -- if one somehow existed, it must be reported
    faithfully (broker state is the source of truth), not coerced to
    positive, so reconciliation can flag it as the anomaly it would be."""
    t = ScriptedTransport()
    t.enqueue(200, [{"symbol": "SPY", "qty": "3", "side": "short",
                    "avg_entry_price": "100.00", "market_value": "-300.00"}])
    positions = adapter(t).positions()
    assert positions[0].qty == -3.0


def test_positions_missing_market_value_falls_back_to_qty_times_avg_price():
    t = ScriptedTransport()
    t.enqueue(200, [{"symbol": "SPY", "qty": "2", "side": "long",
                    "avg_entry_price": "100.00", "market_value": None}])
    positions = adapter(t).positions()
    assert positions[0].market_value == 200.0


def test_positions_request_uses_get_v2_positions():
    t = ScriptedTransport()
    t.enqueue(200, [])
    adapter(t).positions()
    assert t.calls[0]["path"] == "https://paper-api.alpaca.markets/v2/positions"


# ------------------------------------------------------------- open_orders()

def test_open_orders_requests_status_open_explicitly():
    t = ScriptedTransport()
    t.enqueue(200, [])
    adapter(t).open_orders()
    call = t.calls[0]
    assert call["path"] == "https://paper-api.alpaca.markets/v2/orders"
    assert call["params"] == {"status": "open"}


def test_open_orders_maps_a_full_order():
    t = ScriptedTransport()
    t.enqueue(200, [order_json(status="new")])
    orders = adapter(t).open_orders()
    assert orders == [BrokerOrder(
        account_id=ACCT, client_order_id="c1", broker_order_id="alpaca-order-1",
        symbol="SPY", side="BUY", qty=1.0, order_type="LIMIT", time_in_force="DAY",
        limit_price=500.0, status="new", filled_qty=0.0, avg_fill_price=None,
        submitted_at=datetime(2026, 7, 20, 13, 0, tzinfo=timezone.utc), filled_at=None,
    )]


def test_open_orders_maps_a_filled_order_with_fill_fields():
    t = ScriptedTransport()
    t.enqueue(200, [order_json(status="filled", filled_qty="1", filled_avg_price="499.50",
                               filled_at="2026-07-20T13:00:05Z")])
    orders = adapter(t).open_orders()
    assert orders[0].status == "filled"
    assert orders[0].filled_qty == 1.0
    assert orders[0].avg_fill_price == 499.5
    assert orders[0].filled_at == datetime(2026, 7, 20, 13, 0, 5, tzinfo=timezone.utc)


@pytest.mark.parametrize("alpaca_status,expected", [
    ("new", "new"), ("partially_filled", "partially_filled"), ("filled", "filled"),
    ("canceled", "canceled"), ("rejected", "rejected"),
    ("accepted", "new"), ("pending_new", "new"), ("accepted_for_bidding", "new"),
    ("pending_cancel", "new"), ("pending_replace", "new"),
    ("done_for_day", "canceled"), ("expired", "canceled"), ("replaced", "canceled"),
    ("pending_review", "new"), ("stopped", "new"), ("suspended", "new"),
    ("calculated", "new"), ("held", "new"),
])
def test_every_known_alpaca_status_maps_to_this_codebases_vocabulary(alpaca_status, expected):
    t = ScriptedTransport()
    t.enqueue(200, [order_json(status=alpaca_status)])
    orders = adapter(t).open_orders()
    assert orders[0].status == expected


def test_status_map_covers_exactly_the_17_known_alpaca_statuses():
    """Regression guard: if Alpaca's OrderStatus enum gains or loses a
    value, this is the one place that should visibly need updating."""
    known = {"new", "partially_filled", "filled", "done_for_day", "canceled",
            "expired", "replaced", "pending_cancel", "pending_replace",
            "pending_review", "accepted", "pending_new", "accepted_for_bidding",
            "stopped", "rejected", "suspended", "calculated", "held"}
    assert set(STATUS_MAP) == known
    assert all(v in ("new", "partially_filled", "filled", "canceled", "rejected")
              for v in STATUS_MAP.values())


def test_unrecognised_status_raises_unsupported_order_shape():
    t = ScriptedTransport()
    t.enqueue(200, [order_json(status="some_future_status_alpaca_might_add")])
    with pytest.raises(UnsupportedOrderShape):
        adapter(t).open_orders()


def test_notional_only_order_with_no_qty_and_no_filled_qty_raises():
    t = ScriptedTransport()
    t.enqueue(200, [order_json(qty=None, filled_qty=None, status="new")])
    with pytest.raises(UnsupportedOrderShape):
        adapter(t).open_orders()


def test_notional_order_with_a_filled_qty_falls_back_to_that():
    t = ScriptedTransport()
    t.enqueue(200, [order_json(qty=None, filled_qty="0.5", status="partially_filled")])
    orders = adapter(t).open_orders()
    assert orders[0].qty == 0.5


# --------------------------------------------------------- get_by_client_id()

def test_get_by_client_id_maps_a_found_order():
    t = ScriptedTransport()
    t.enqueue(200, order_json(client_order_id="c9"))
    order = adapter(t).get_by_client_id("c9")
    assert order.client_order_id == "c9"
    call = t.calls[0]
    assert call["path"] == "https://paper-api.alpaca.markets/v2/orders:by_client_order_id"
    assert call["params"] == {"client_order_id": "c9"}


def test_get_by_client_id_returns_none_on_404():
    t = ScriptedTransport()
    t.enqueue(404, {"code": 40410000, "message": "order not found"})
    assert adapter(t).get_by_client_id("nope") is None


# ------------------------------------------------------------- sessions()

def test_sessions_delegates_to_market_calendar_not_a_second_implementation():
    from agent import market_calendar as mc
    t = ScriptedTransport()  # no HTTP call expected
    result = adapter(t).sessions(date(2026, 11, 27), 5)
    assert result == mc.trailing_sessions(date(2026, 11, 27), 5)
    assert t.calls == []   # sessions() never touches the network


# --------------------------------------------------------- supported_matrix()

def test_supported_matrix_returns_a_populated_dict():
    matrix = adapter().supported_matrix()
    assert "order_type" in matrix and "MARKET" in matrix["order_type"]
    assert "time_in_force" in matrix and "DAY" in matrix["time_in_force"]


def test_supported_matrix_session_reflects_the_real_capture():
    """FINDING (§13 probe, 2026-07-27 capture: configurations.json,
    assets.json): `disable_overnight_trading: false` at the account level,
    plus `overnight_tradable`/`fractional_eh_enabled` on every one of the
    three probed assets (SPY, QQQ, AAPL), contradicts the old
    ["REGULAR"]-only guess. Updated to match `agent.policy.initial_policy`'s
    own REGULAR/EXTENDED/OVERNIGHT session vocabulary -- this is an
    empirical BROKER-capability fact, independent of and not a proposal to
    change the capability policy's own default of disabling EXTENDED/
    OVERNIGHT (Appendix E)."""
    matrix = adapter().supported_matrix()
    assert set(matrix["session"]) == {"REGULAR", "EXTENDED", "OVERNIGHT"}


def test_supported_matrix_fractional_order_types_remain_an_unverified_guess():
    """`fractional_trading: true` (configurations.json) and
    `fractionable: true` (assets.json, all three probed symbols) confirm
    fractional trading is enabled and available -- but neither endpoint
    says WHICH order types accept a fractional quantity, so this specific
    list is unchanged: still a documented guess, not a confirmed one."""
    matrix = adapter().supported_matrix()
    assert matrix["fractional"] == ["MARKET", "LIMIT"]


# ------------------------------------------------------------------ submit()

def staged_order(gk, *, client_order_id="c1", side="BUY", qty=1.0, price=100.0,
                 order_type="LIMIT", limit_price=100.0):
    portfolio = PortfolioState(account_id=ACCT, nlv=10000.0, settled_cash=10000.0)
    return gk.stage(client_order_id=client_order_id, symbol="SPY", side=side,
                    order_type=order_type, time_in_force="DAY", portfolio=portfolio,
                    now=datetime(2026, 7, 20, 15, 0, tzinfo=timezone.utc),
                    posture="CASH", qty=qty, price=price, limit_price=limit_price)


def gatekeeper():
    from agent.accounts import AccountType
    from agent.daytrade import DayTradeGuard
    return Gatekeeper(account_id=ACCT, account_type=AccountType.TAXABLE,
                      capability_policy=initial_policy(), risk_policy=_risk_policy(),
                      day_trade_guard=DayTradeGuard(account_id=ACCT), live=False)


def _risk_policy():
    from agent.risk import RiskPolicy
    return RiskPolicy("t", max_position_pct=50.0, max_sector_pct=100.0,
                      min_settled_cash_pct_of_nlv=0.0, min_absolute_settled_cash=0.0)


def approved_token(s, *, svc=None, now=None,
                   shown_delta=timedelta(seconds=15),
                   token_id="t1", request_id="r1"):
    """Mints a token that exactly matches StagedOrder `s` -- every submit()
    call in this file needs one now that the approval-token requirement
    applies to paper adapters too, not just live ones (require-a-token-in-
    paper unit, 2026-08-09; agent/broker/base.py's own `if self.is_live:`
    gate around the token check is gone). Derived from the StagedOrder
    itself, rather than re-specifying matching fields by hand, so the
    fingerprint can never accidentally drift from what was actually
    staged.

    `now` defaults to a FRESH `datetime.now(timezone.utc)` read at call
    time (never a fixed literal): `AlpacaPaperAdapter` -- unlike
    `SimulatorBroker` elsewhere in this codebase's tests -- has no
    injectable fake clock, so `submit()`'s own `self.clock()` call always
    returns the real wall-clock instant. A token minted against a fixed
    2026-07-20 literal expired the moment this test suite was run on any
    later date (found running this fix on 2026-08-09)."""
    now = now or datetime.now(timezone.utc)
    svc = svc or ApprovalService(expiration=timedelta(minutes=30),
                                 min_display=timedelta(seconds=10), max_per_day=4)
    fp = order_fingerprint(symbol=s.symbol, side=s.side, qty=s.authorized_qty,
                           order_type=s.order_type, time_in_force=s.time_in_force,
                           limit_price=s.limit_price, lot_id=s.lot_id)
    return svc.approve(token_id=token_id, request_id=request_id, fingerprint=fp,
                       price_at_analysis=s.limit_price or 0.0, shown_at=now - shown_delta,
                       now=now, symbol=s.symbol, side=s.side, qty=s.authorized_qty,
                       order_type=s.order_type, time_in_force=s.time_in_force,
                       limit_price=s.limit_price, lot_id=s.lot_id)


def test_submit_checks_idempotency_first_before_any_post():
    t = ScriptedTransport()
    t.enqueue(200, order_json(client_order_id="c1", status="new"))  # get_by_client_id hit
    gk = gatekeeper()
    a = adapter(t)
    a.attach_staging_key(gk.signing_key)
    staged = staged_order(gk)
    order = a.submit(staged, approval_token=approved_token(staged))
    assert order.client_order_id == "c1"
    # Only the one GET -- no POST at all, since it already existed.
    assert len(t.calls) == 1
    assert t.calls[0]["method"] == "GET"


def test_submit_posts_with_client_order_id_when_not_already_known():
    t = ScriptedTransport()
    t.enqueue(404, {"message": "not found"})            # get_by_client_id: not found
    t.enqueue(200, order_json(client_order_id="c1", status="filled"))  # POST response
    gk = gatekeeper()
    a = adapter(t)
    a.attach_staging_key(gk.signing_key)
    staged = staged_order(gk)
    order = a.submit(staged, approval_token=approved_token(staged))
    assert order.status == "filled"
    post_call = t.calls[1]
    assert post_call["method"] == "POST"
    assert post_call["path"] == "https://paper-api.alpaca.markets/v2/orders"
    assert post_call["json_body"]["client_order_id"] == "c1"
    assert post_call["json_body"]["side"] == "buy"
    assert post_call["json_body"]["symbol"] == "SPY"


def test_submit_still_requires_a_valid_staging_key_first():
    """BrokerAdapter's existing gate -- unchanged. Proven here so this
    adapter doesn't accidentally bypass it."""
    t = ScriptedTransport()
    gk = gatekeeper()
    a = adapter(t)  # no staging key attached
    staged = staged_order(gk)
    with pytest.raises(StagingKeyUnset):
        a.submit(staged)
    assert t.calls == []  # never reaches the network without a valid signature


def test_submit_timeout_raises_ambiguous_order_state_not_a_retry():
    t = ScriptedTransport()
    t.enqueue(404, {"message": "not found"})   # get_by_client_id: not found
    t.enqueue_error(TransportTimeout("slow"))  # the POST times out
    gk = gatekeeper()
    a = adapter(t, max_retries=3)   # even with retries configured for reads
    a.attach_staging_key(gk.signing_key)
    staged = staged_order(gk)
    with pytest.raises(AmbiguousOrderState, match="c1"):
        a.submit(staged, approval_token=approved_token(staged))
    # Exactly 2 calls -- the idempotency GET, and ONE POST attempt. A write
    # never retries, regardless of http_max_retries.
    assert len(t.calls) == 2


def test_submit_duplicate_422_resolves_via_get_by_client_id_not_raised():
    """A race: something else creates the same client_order_id between our
    idempotency check and our POST. Alpaca returns 422; we resolve it via
    get_by_client_id rather than treating 422 as a hard failure."""
    t = ScriptedTransport()
    t.enqueue(404, {"message": "not found"})     # initial idempotency check
    t.enqueue(422, {"message": "client order id already exists"})  # the POST
    t.enqueue(200, order_json(client_order_id="c1", status="filled"))  # resolve
    gk = gatekeeper()
    a = adapter(t)
    a.attach_staging_key(gk.signing_key)
    staged = staged_order(gk)
    order = a.submit(staged, approval_token=approved_token(staged))
    assert order.status == "filled"


def test_submit_genuine_422_with_no_resolvable_order_raises():
    t = ScriptedTransport()
    t.enqueue(404, {"message": "not found"})
    t.enqueue(422, {"message": "some other rejection"})
    t.enqueue(404, {"message": "not found"})   # resolve attempt also finds nothing
    gk = gatekeeper()
    a = adapter(t)
    a.attach_staging_key(gk.signing_key)
    staged = staged_order(gk)
    with pytest.raises(AlpacaError):
        a.submit(staged, approval_token=approved_token(staged))


# ------------------------------------------------------------------ cancel()

def cancel_staged_order(gk, *, client_order_id="c1", symbol="SPY", order_type="LIMIT",
                        limit_price=100.0):
    """A CANCEL StagedOrder for client_order_id, built the same way
    agent/pipeline.py's own CANCEL path is exercised elsewhere (see
    tests/test_cancel_gate.py) -- through the public Gatekeeper.stage(),
    not by hand-constructing and re-signing a StagedOrder."""
    portfolio = PortfolioState(account_id=ACCT, nlv=10000.0, settled_cash=10000.0)
    return gk.stage(client_order_id=client_order_id, symbol=symbol, side="CANCEL",
                    order_type=order_type, time_in_force="DAY", portfolio=portfolio,
                    now=datetime(2026, 7, 20, 15, 0, tzinfo=timezone.utc),
                    posture="CASH", limit_price=limit_price)


def test_cancel_looks_up_broker_order_id_then_deletes_by_it():
    t = ScriptedTransport()
    t.enqueue(200, order_json(client_order_id="c1", status="new"))  # get_by_client_id
    t.enqueue(204, {})                                              # DELETE
    t.enqueue(200, order_json(client_order_id="c1", status="canceled"))  # re-fetch
    gk = gatekeeper()
    a = adapter(t)
    a.attach_staging_key(gk.signing_key)
    cancel_order = cancel_staged_order(gk)
    result = a.cancel(cancel_order)
    assert result.status == "canceled"
    delete_call = t.calls[1]
    assert delete_call["method"] == "DELETE"
    assert delete_call["path"] == "https://paper-api.alpaca.markets/v2/orders/alpaca-order-1"


def test_cancel_of_an_unknown_order_returns_none():
    t = ScriptedTransport()
    t.enqueue(404, {"message": "not found"})
    gk = gatekeeper()
    a = adapter(t)
    a.attach_staging_key(gk.signing_key)
    cancel_order = cancel_staged_order(gk)
    assert a.cancel(cancel_order) is None
    assert len(t.calls) == 1  # never attempted a DELETE for a nonexistent order


def test_cancel_timeout_raises_ambiguous_order_state():
    t = ScriptedTransport()
    t.enqueue(200, order_json(client_order_id="c1", status="new"))
    t.enqueue_error(TransportTimeout("slow"))
    gk = gatekeeper()
    a = adapter(t, max_retries=3)
    a.attach_staging_key(gk.signing_key)
    cancel_order = cancel_staged_order(gk)
    with pytest.raises(AmbiguousOrderState):
        a.cancel(cancel_order)
    assert len(t.calls) == 2  # the lookup, plus exactly one DELETE attempt -- no retry


# ------------------------------------------------------------------ fills()
# Account Activities' TradeActivity (alpaca-py's own model, not this
# codebase's guess) carries no client_order_id -- only Alpaca's own
# order_id. fills() must resolve that via GET /v2/orders/{order_id}, once
# per distinct order_id, not once per activity.

def activity_json(**over):
    base = dict(id="20260720000000000::aaaa-1", account_id=ACCT,
               activity_type="FILL", transaction_time="2026-07-20T13:00:05Z",
               type="fill", price="499.50", qty="1", side="buy", symbol="SPY",
               leaves_qty="0", order_id="alpaca-order-1", cum_qty="1",
               order_status="filled")
    base.update(over)
    return base


def test_fills_requests_the_fill_activity_type_with_ascending_direction():
    t = ScriptedTransport()
    t.enqueue(200, [])
    adapter(t).fills()
    call = t.calls[0]
    assert call["path"] == "https://paper-api.alpaca.markets/v2/account/activities/FILL"
    assert call["params"]["direction"] == "asc"


def test_fills_maps_price_qty_and_cum_qty_as_reported_not_averaged():
    t = ScriptedTransport()
    t.enqueue(200, [activity_json(qty="1", price="499.50", cum_qty="1")])
    t.enqueue(200, order_json(client_order_id="c1"))
    fills = adapter(t).fills()
    assert fills == [Execution(
        execution_id="20260720000000000::aaaa-1", account_id=ACCT,
        client_order_id="c1", symbol="SPY", side="BUY", qty=1.0, price=499.5,
        cum_qty=1.0, filled_at=datetime(2026, 7, 20, 13, 0, 5, tzinfo=timezone.utc),
    )]


def test_fills_resolves_client_order_id_via_the_order_lookup():
    t = ScriptedTransport()
    t.enqueue(200, [activity_json(order_id="alpaca-order-9")])
    t.enqueue(200, order_json(client_order_id="c9"))
    fills = adapter(t).fills()
    assert fills[0].client_order_id == "c9"
    lookup_call = t.calls[1]
    assert lookup_call["path"] == "https://paper-api.alpaca.markets/v2/orders/alpaca-order-9"


def test_fills_resolves_client_order_id_once_per_distinct_order_id():
    t = ScriptedTransport()
    t.enqueue(200, [
        activity_json(id="a1", order_id="alpaca-order-1", qty="1", cum_qty="1"),
        activity_json(id="a2", order_id="alpaca-order-1", qty="1", cum_qty="2"),
    ])
    t.enqueue(200, order_json(client_order_id="c1"))
    fills = adapter(t).fills()
    assert [f.client_order_id for f in fills] == ["c1", "c1"]
    # one activities page + exactly one order lookup, not two
    assert len(t.calls) == 2


def test_fills_pages_forward_until_a_short_page_is_returned():
    t = ScriptedTransport()
    full_page = [activity_json(id=f"a{i}", order_id="alpaca-order-1") for i in range(100)]
    t.enqueue(200, full_page)
    t.enqueue(200, [activity_json(id="a100", order_id="alpaca-order-1")])
    t.enqueue(200, order_json(client_order_id="c1"))
    fills = adapter(t).fills()
    assert len(fills) == 101
    first_page_call, second_page_call, _ = t.calls
    assert "page_token" not in first_page_call["params"]
    assert second_page_call["params"]["page_token"] == "a99"


def test_fills_stops_paging_on_an_empty_page():
    t = ScriptedTransport()
    t.enqueue(200, [])
    fills = adapter(t).fills()
    assert fills == []
    assert len(t.calls) == 1


# --------------------------------------------- non_fill_activities (Commit 3,
# cash-event quarantine unit). Real shape confirmed 2026-07-30 against a real
# Alpaca paper account: scripts/fixtures/activities_since.json -- a CAT
# regulatory fee, dated the day of a fractional SPY buy, posted overnight.

def cat_fee_json(**over):
    base = dict(id="20260728000000000::de3745eb-7d16-4bf3-9514-234693d9f84e",
               activity_type="FEE", activity_sub_type="CAT",
               created_at="2026-07-29T00:07:16.323361Z", currency="USD",
               date="2026-07-28",
               description="CAT fee for proceed of 1 trades on 2026-07-28 by PA3XZX944LRR",
               net_amount="-0.01", status="executed")
    base.update(over)
    return base


def test_non_fill_activities_hits_the_general_endpoint_not_fill():
    t = ScriptedTransport()
    t.enqueue(200, [])
    adapter(t).non_fill_activities()
    call = t.calls[0]
    assert call["path"] == "https://paper-api.alpaca.markets/v2/account/activities"
    assert call["params"]["direction"] == "asc"


def test_non_fill_activities_maps_the_real_cat_fee_shape():
    t = ScriptedTransport()
    t.enqueue(200, [cat_fee_json()])
    [activity] = adapter(t).non_fill_activities()
    assert activity.activity_id == "20260728000000000::de3745eb-7d16-4bf3-9514-234693d9f84e"
    assert activity.account_id == ACCT
    assert activity.activity_type == "FEE"
    assert activity.activity_sub_type == "CAT"
    assert activity.net_amount == Decimal("-0.01")
    assert activity.date == date(2026, 7, 28)
    # created_at (added 2026-07-31): the broker's own posting instant --
    # a full day after `date` above for this real CAT fee, exactly the
    # gap agent/broker/base.py's own docstring documents.
    assert activity.created_at == datetime(2026, 7, 29, 0, 7, 16, 323361,
                                           tzinfo=timezone.utc)
    assert activity.symbol is None
    assert activity.description == (
        "CAT fee for proceed of 1 trades on 2026-07-28 by PA3XZX944LRR"
    )


def test_non_fill_activities_excludes_fill_type_rows():
    """FILL is already covered by fills() -- non_fill_activities must not
    double-report it, even though the general endpoint (no activity_types
    filter, matching scripts/alpaca_probe.py's own no-allowlist choice)
    returns it right alongside everything else."""
    t = ScriptedTransport()
    t.enqueue(200, [cat_fee_json(), activity_json()])
    activities = adapter(t).non_fill_activities()
    assert [a.activity_type for a in activities] == ["FEE"]


def test_non_fill_activities_carries_a_symbol_when_the_broker_reports_one():
    t = ScriptedTransport()
    t.enqueue(200, [cat_fee_json(id="d1", activity_type="DIV", activity_sub_type=None,
                                symbol="SPY", net_amount="1.23",
                                description="dividend")])
    [activity] = adapter(t).non_fill_activities()
    assert activity.symbol == "SPY"
    assert activity.activity_sub_type is None


def test_non_fill_activities_pages_forward_until_a_short_page_is_returned():
    t = ScriptedTransport()
    full_page = [cat_fee_json(id=f"d{i}") for i in range(100)]
    t.enqueue(200, full_page)
    t.enqueue(200, [cat_fee_json(id="d100")])
    activities = adapter(t).non_fill_activities()
    assert len(activities) == 101
    first_page_call, second_page_call = t.calls
    assert "page_token" not in first_page_call["params"]
    assert second_page_call["params"]["page_token"] == "d99"


def test_non_fill_activities_stops_paging_on_an_empty_page():
    t = ScriptedTransport()
    t.enqueue(200, [])
    activities = adapter(t).non_fill_activities()
    assert activities == []
    assert len(t.calls) == 1


def test_non_fill_activities_against_the_real_committed_fixture():
    """Found real, 2026-07-31: the first unattended launchd run crashed
    with `KeyError: 'created_at'` -- the real FILL row in
    scripts/fixtures/activities_since.json carries `transaction_time`, not
    `created_at` (it wasn't in this file's fixture-driven suite at all,
    every prior test built its own FILL dict by hand -- activity_json(),
    used above, doesn't have created_at either, but that's still a
    hand-built dict, not the actual committed evidence). Drives
    non_fill_activities() against the REAL captured payload directly, not
    a hand-built one, per the standing rule that the fixture exists so
    real broker shapes are testable."""
    import json
    from pathlib import Path
    raw = json.loads(
        (Path(__file__).parent.parent / "scripts/fixtures/activities_since.json")
        .read_text()
    )["activities"]
    assert raw[2]["activity_type"] == "FILL" and "created_at" not in raw[2]   # sanity on the fixture itself

    t = ScriptedTransport()
    t.enqueue(200, raw)
    activities = adapter(t).non_fill_activities()

    assert [a.activity_type for a in activities] == ["JNLC", "FEE"]
    assert activities[0].created_at == datetime(2026, 7, 27, 13, 0, 50, 193924,
                                                tzinfo=timezone.utc)
    assert activities[1].created_at == datetime(2026, 7, 29, 0, 7, 16, 323361,
                                                tzinfo=timezone.utc)


def test_non_fill_activities_tolerates_a_missing_created_at_on_a_non_fill_row():
    """Not real evidence -- the fixture only confirms created_at present
    for JNLC and FEE, two of the ~35 documented activity types
    (agent/cash_event_quarantine.py's own module docstring). A non-FILL
    row missing created_at must not crash; it is reported as
    created_at=None, never guessed at (e.g. defaulted to `now`)."""
    row = cat_fee_json(activity_type="DIV", activity_sub_type=None,
                       net_amount="1.23", description="dividend")
    del row["created_at"]
    t = ScriptedTransport()
    t.enqueue(200, [row])
    [activity] = adapter(t).non_fill_activities()
    assert activity.created_at is None


# ------------------- TERMINAL_ORDER_STATUSES reconciled against STATUS_MAP
# (2026-07-30, "nothing ever closes an OrderRecord" fix). Not a new
# vocabulary -- STATUS_MAP already collapses every raw Alpaca status into
# exactly BrokerOrder's five canonical values; TERMINAL_ORDER_STATUSES names
# the three of those five that will never change again.

def test_terminal_order_statuses_is_exactly_three_of_status_maps_five_canonical_values():
    canonical = set(STATUS_MAP.values())
    assert canonical == {"new", "partially_filled", "filled", "canceled", "rejected"}
    assert TERMINAL_ORDER_STATUSES == {"filled", "canceled", "rejected"}
    assert TERMINAL_ORDER_STATUSES < canonical
    assert canonical - TERMINAL_ORDER_STATUSES == {"new", "partially_filled"}


# ===========================================================================
# REAL-SHAPE CONTRACT TESTS (live-adapter-parsing-failure unit, 2026-08-13).
#
# ROOT CAUSE this section proves closed: account()/positions()/open_orders()/
# fills() each discarded the HTTP status `_request` already returns and
# parsed the response body directly as the endpoint's success shape. A
# non-2xx response (Alpaca's own documented error shape: a small JSON
# OBJECT) then produced, respectively: `KeyError: 'equity'` (account -- a
# dict missing that key), `TypeError: string indices must be integers`
# (positions/fills -- iterating a DICT yields its KEYS, which are strings,
# then indexing a string with a non-integer key), `AttributeError: 'str'
# object has no attribute 'get'` (open_orders -- same string-key iteration,
# but _to_broker_order calls .get() first) -- this is EXACTLY the real Mac
# failure_sentinel signature reported (x386, "string indices must be
# integers") from a real diagnose_runtime.py run against PA3XZX944LRR.
#
# Every fixture below marked REAL CAPTURE is drawn verbatim from this
# codebase's own committed evidence (scripts/fixtures/{account,positions,
# orders,activities_since}.json -- probed against the same real paper
# account on 2026-07-27/2026-07-30, `"status": 200` in every case) -- not
# hand-invented shapes. These confirm Alpaca's SUCCESS wire shape is exactly
# what this adapter's field-mapping already assumed; nothing about that
# mapping needed to change. What changed is that a non-2xx / wrong-type /
# malformed response is now caught BEFORE field access, as a named
# `AlpacaResponseError`, instead of falling through to a bare stdlib
# exception with no endpoint context.
# ===========================================================================

import json as _json
from pathlib import Path as _Path

from agent.broker.alpaca import AlpacaResponseError

_FIXTURES_DIR = _Path(__file__).resolve().parent.parent / "scripts" / "fixtures"


def _real_fixture_body(name: str):
    return _json.loads((_FIXTURES_DIR / name).read_text())["body"]


# ------------------------------------------------ 1. account() success shapes

def test_account_against_the_real_committed_capture():
    """REAL CAPTURE: scripts/fixtures/account.json, probed 2026-07-27
    against PA3XZX944LRR, status 200. 36 top-level fields; this adapter
    only reads five of them (plus the two absent-vs-present PDT fields
    covered elsewhere) -- proves the real wire body, unmodified, parses
    clean through account() end to end."""
    body = _real_fixture_body("account.json")
    t = ScriptedTransport()
    t.enqueue(200, body)
    snap = adapter(t).account()
    assert snap.equity == Decimal("500.12")
    assert snap.cash == Decimal("480")
    assert snap.buying_power == Decimal("480")
    assert snap.multiplier == Decimal("1")


# ----------------------------------------------- 2. positions() success shapes

def test_positions_against_the_real_committed_capture():
    """REAL CAPTURE: scripts/fixtures/positions.json -- a real fractional
    SPY position, status 200."""
    body = _real_fixture_body("positions.json")
    t = ScriptedTransport()
    t.enqueue(200, body)
    [pos] = adapter(t).positions()
    assert pos.symbol == "SPY"
    assert pos.qty == Decimal("0.027087234")
    assert pos.avg_price == Decimal("737.986")
    assert pos.market_value == Decimal("20.124691")


def test_positions_no_position_response_is_an_empty_list_not_an_error():
    t = ScriptedTransport()
    t.enqueue(200, [])
    assert adapter(t).positions() == []


# ---------------------------------------------- 3. open_orders() success shapes

def test_open_orders_against_the_real_committed_capture():
    """REAL CAPTURE: scripts/fixtures/orders.json -- a real FILLED,
    notional-originated order (qty=null, filled_qty populated: exactly the
    `_to_broker_order` notional fallback path), status 200."""
    body = _real_fixture_body("orders.json")
    t = ScriptedTransport()
    t.enqueue(200, body)
    [order] = adapter(t).open_orders()
    assert order.client_order_id == "732f0667-fb47-43f6-babd-7c0fe64ece18"
    assert order.status == "filled"
    assert order.qty == Decimal("0.027087234")
    assert order.avg_fill_price == Decimal("737.986")


def test_open_orders_no_open_orders_response_is_an_empty_list_not_an_error():
    t = ScriptedTransport()
    t.enqueue(200, [])
    assert adapter(t).open_orders() == []


# -------------------------------------------------- 4. fills() success shapes

def test_fills_against_the_real_committed_activities_capture():
    """REAL CAPTURE: scripts/fixtures/activities_since.json's own FILL row
    -- the actual, already-quarantine-admitted execution
    (20260728104251412::37042727-dfba-4cac-a1d7-607636cd4346, order_id
    91dcb2f4-315c-4c39-8211-1f71ed7d49a9) this unit's own item 6 is about.
    Proves fills() parses that exact real row end to end once the order-id
    lookup resolves."""
    activities = _json.loads(
        (_FIXTURES_DIR / "activities_since.json").read_text())["activities"]
    fill_row = next(a for a in activities if a["activity_type"] == "FILL")
    t = ScriptedTransport()
    t.enqueue(200, [fill_row])
    t.enqueue(200, order_json(client_order_id="c-real"))
    [execution] = adapter(t).fills()
    assert execution.execution_id == (
        "20260728104251412::37042727-dfba-4cac-a1d7-607636cd4346")
    assert execution.qty == Decimal("0.027087234")
    assert execution.price == Decimal("737.986")
    assert execution.client_order_id == "c-real"


# -------------------------------------------- 5. malformed / wrong-type shapes

def test_account_on_wrong_top_level_type_raises_alpaca_response_error_not_a_crash():
    """The real defect's actual trigger shape for account(): a non-2xx
    error response IS a dict (Alpaca's documented error shape), so this
    exercises the OTHER direction -- a genuinely wrong top-level type
    (e.g. a list) on an otherwise-200 response -- as the belt-and-suspenders
    check module docstring describes."""
    t = ScriptedTransport()
    t.enqueue(200, [{"unexpected": "list, not an object"}])
    with pytest.raises(AlpacaResponseError):
        adapter(t).account()


def test_positions_on_wrong_top_level_type_raises_not_a_bare_typeerror():
    """THIS IS THE ACTUAL REAL DEFECT, REPRODUCED: before this fix, a dict
    body (e.g. Alpaca's own error-response shape) reaching positions()'s old
    `for p in data: p["qty"]` would iterate the dict's KEYS (strings) and
    then index a string with a non-integer key -- exactly `TypeError: string
    indices must be integers`, the real failure_sentinel signature (x386)
    from the real Mac run. Now caught by `_expect_list` before any
    iteration, as a named, endpoint-labelled `AlpacaResponseError`."""
    t = ScriptedTransport()
    t.enqueue(200, {"code": 40110000, "message": "not really an error status, "
                                                  "but still the wrong shape"})
    with pytest.raises(AlpacaResponseError, match="GET /v2/positions"):
        adapter(t).positions()


def test_open_orders_on_wrong_top_level_type_raises_not_a_bare_attributeerror():
    """The real defect's other exact signature: `AttributeError: 'str'
    object has no attribute 'get'` -- `_to_broker_order`'s old
    `o.get("qty")` called on a STRING (one of the error dict's own keys,
    yielded by iterating it as if it were a list of order objects)."""
    t = ScriptedTransport()
    t.enqueue(200, {"code": 50010000, "message": "wrong shape for open_orders"})
    with pytest.raises(AlpacaResponseError, match="GET /v2/orders"):
        adapter(t).open_orders()


def test_positions_element_that_is_not_an_object_raises_named_error():
    t = ScriptedTransport()
    t.enqueue(200, ["not-a-position-object"])
    with pytest.raises(AlpacaResponseError):
        adapter(t).positions()


def test_open_orders_element_that_is_not_an_object_raises_named_error():
    t = ScriptedTransport()
    t.enqueue(200, ["not-an-order-object"])
    with pytest.raises(AlpacaResponseError):
        adapter(t).open_orders()


# ------------------------------------------------- 6. API error object shapes

def test_account_on_a_documented_alpaca_error_object_raises_named_error_not_keyerror():
    """THE OTHER HALF OF THE REAL DEFECT: a non-2xx status whose body IS
    Alpaca's own documented small error object (`{"code": ..., "message":
    ...}`) -- account()'s old `data["equity"]` on this dict raised a bare
    `KeyError: 'equity'`, the real failure_sentinel signature reported for
    account(). Now caught by `_ensure_ok` on the status BEFORE any field
    access, regardless of what the error body happens to contain."""
    t = ScriptedTransport()
    t.enqueue(401, {"code": 40110000, "message": "authentication failed"})
    with pytest.raises(AlpacaResponseError, match="GET /v2/account"):
        adapter(t).account()


def test_positions_on_a_documented_alpaca_error_object_raises_named_error():
    t = ScriptedTransport()
    t.enqueue(401, {"code": 40110000, "message": "authentication failed"})
    with pytest.raises(AlpacaResponseError, match="GET /v2/positions"):
        adapter(t).positions()


def test_open_orders_on_a_documented_alpaca_error_object_raises_named_error():
    t = ScriptedTransport()
    t.enqueue(401, {"code": 40110000, "message": "authentication failed"})
    with pytest.raises(AlpacaResponseError, match="GET /v2/orders"):
        adapter(t).open_orders()


def test_fills_on_a_documented_alpaca_error_object_raises_named_error():
    t = ScriptedTransport()
    t.enqueue(401, {"code": 40110000, "message": "authentication failed"})
    with pytest.raises(AlpacaResponseError, match="activities/FILL"):
        adapter(t).fills()


def test_get_by_client_id_404_is_still_none_not_an_error():
    """UNCHANGED special case: 404 on THIS endpoint specifically means "no
    such order", not a generic error -- must still short-circuit to `None`
    before `_ensure_ok`, exactly as before this fix."""
    t = ScriptedTransport()
    t.enqueue(404, {"code": 40410000, "message": "order not found"})
    assert adapter(t).get_by_client_id("nope") is None


def test_get_by_client_id_non_404_error_raises_named_error():
    t = ScriptedTransport()
    t.enqueue(500, {"code": 50010000, "message": "internal error"})
    with pytest.raises(AlpacaResponseError, match="by_client_order_id"):
        adapter(t).get_by_client_id("c1")


# -------------------------------------------------------- 7. HTTP error shapes

def test_account_on_http_error_with_a_non_dict_body_still_raises_named_error():
    """A non-2xx status caught by `_ensure_ok` BEFORE `_expect_dict` even
    runs -- proves the ordering (status check first) holds even when the
    error body itself isn't the documented shape (e.g. a load balancer's
    own HTML/plain-text error page, not Alpaca's JSON at all)."""
    t = ScriptedTransport()
    t.enqueue(503, "Service Unavailable")
    with pytest.raises(AlpacaResponseError, match="GET /v2/account"):
        adapter(t).account()


def test_positions_on_http_error_with_empty_dict_body_raises_named_error():
    t = ScriptedTransport()
    t.enqueue(500, {})
    with pytest.raises(AlpacaResponseError, match="GET /v2/positions"):
        adapter(t).positions()


# ------------------------------------------------------------ 8. empty body

def test_account_on_a_200_with_an_empty_object_body_raises_named_error_not_keyerror():
    """`UrllibTransport`/`ScriptedTransport` both decode an empty body as
    `{}` (see agent/broker/transport.py) -- a genuinely empty 200 response
    is therefore indistinguishable, at this layer, from "a dict missing
    every field," which is exactly what `_expect_dict` + the per-field
    try/except already handles: no crash, a named, endpoint-labelled
    error."""
    t = ScriptedTransport()
    t.enqueue(200, {})
    with pytest.raises(AlpacaResponseError, match="GET /v2/account"):
        adapter(t).account()


def test_positions_on_a_200_with_an_empty_object_body_raises_named_error():
    """An empty-body 200 for a LIST endpoint decodes to `{}`, not `[]` --
    `_expect_list` catches the type mismatch rather than this silently
    behaving like "no positions" (which would be indistinguishable from a
    real empty portfolio -- exactly the kind of silent-empty Appendix E's
    fail-safe bias forbids)."""
    t = ScriptedTransport()
    t.enqueue(200, {})
    with pytest.raises(AlpacaResponseError, match="GET /v2/positions"):
        adapter(t).positions()


# -------------------------------------------------------- 9. unexpected wrapper

def test_account_wrapped_in_an_unexpected_envelope_raises_named_error():
    """A hypothetical future envelope change (e.g. `{"account": {...}}`
    instead of the bare object this adapter's own real capture confirms
    Alpaca actually sends) -- the top-level IS a dict, so `_expect_dict`
    passes, but `data["equity"]` is still missing at THIS level; the
    per-field try/except catches the resulting KeyError and re-raises it as
    a named, endpoint-labelled error rather than propagating a bare
    KeyError."""
    t = ScriptedTransport()
    t.enqueue(200, {"account": account_json()})
    with pytest.raises(AlpacaResponseError, match="GET /v2/account"):
        adapter(t).account()


# ------------------------------------------- 10. fills()/non_fill_activities()
# pagination-loop and per-object validation (not just the first page)

def test_fills_pagination_loop_validates_every_page_not_just_the_first():
    """Page 1 is a FULL page (100 == page_size), so the loop keeps paging
    -- proves page 2's own failure is caught too, not just a hypothetical
    check on page 1 that a short/empty final page would never exercise."""
    t = ScriptedTransport()
    t.enqueue(200, [activity_json(id=f"a{i}", order_id="alpaca-order-1")
                    for i in range(100)])
    t.enqueue(401, {"code": 40110000, "message": "authentication failed"})  # page 2 fails
    with pytest.raises(AlpacaResponseError, match="activities/FILL"):
        adapter(t).fills()


def test_non_fill_activities_pagination_loop_validates_every_page():
    t = ScriptedTransport()
    t.enqueue(200, [cat_fee_json(id=f"d{i}") for i in range(100)])
    t.enqueue(401, {"code": 40110000, "message": "authentication failed"})
    with pytest.raises(AlpacaResponseError, match="/v2/account/activities"):
        adapter(t).non_fill_activities()


def test_non_fill_activities_element_that_is_not_an_object_is_skipped_not_crashed():
    """`non_fill_activities`'s own FILL-exclusion filter already guards with
    `isinstance(a, dict)` before `.get(...)` -- a non-dict element is
    excluded by the filter (same defensive posture as the FILL-type
    exclusion itself), not force-fed into `_to_account_activity`, which
    would raise. This is intentionally more permissive than positions/
    open_orders (where a non-dict element is a hard, immediate error)
    because the exclusion filter runs first, by construction, for every
    element, not just malformed ones."""
    t = ScriptedTransport()
    t.enqueue(200, ["not-an-activity-object", cat_fee_json()])
    activities = adapter(t).non_fill_activities()
    assert [a.activity_type for a in activities] == ["FEE"]


def test_client_order_id_for_on_error_status_raises_named_error():
    t = ScriptedTransport()
    t.enqueue(200, [activity_json(order_id="alpaca-order-9")])
    t.enqueue(401, {"code": 40110000, "message": "authentication failed"})
    with pytest.raises(AlpacaResponseError, match="/v2/orders/alpaca-order-9"):
        adapter(t).fills()


# ---------------------------------------------- 11. no silent empty, ever

def test_none_of_the_new_validation_helpers_ever_return_a_silent_default():
    """Appendix E's fail-safe-to-NO-TRADE invariant, applied directly:
    every failure mode exercised above raises `AlpacaResponseError` -- none
    of them return an empty list, a zeroed AccountSnapshot, or any other
    stand-in value. This test is a single, explicit cross-check tying that
    property back to the invariant by name, on top of the many individual
    pytest.raises(...) assertions above."""
    scenarios = [
        lambda t: adapter(t).account(),
        lambda t: adapter(t).positions(),
        lambda t: adapter(t).open_orders(),
        lambda t: adapter(t).fills(),
    ]
    for scenario in scenarios:
        t = ScriptedTransport()
        t.enqueue(401, {"code": 40110000, "message": "authentication failed"})
        # If this ever silently returned {}/[]/None instead of raising,
        # pytest.raises itself would fail here with "DID NOT RAISE" -- that
        # failure mode IS the assertion this test makes.
        with pytest.raises(AlpacaResponseError):
            scenario(t)
