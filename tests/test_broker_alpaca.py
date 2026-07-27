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

from datetime import date, datetime, timezone

import pytest

from agent.accounts import BrokerCredentials, CrossAccountError
from agent.broker.alpaca import (STATUS_MAP, AlpacaError, AlpacaPaperAdapter,
                                 AmbiguousOrderState, UnsupportedOrderShape)
from agent.broker.base import AccountSnapshot, BrokerOrder, Position, StagingKeyUnset
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
    assert snap.equity == 512.34
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
    assert snap.settled_cash == 123.45
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


def test_submit_checks_idempotency_first_before_any_post():
    t = ScriptedTransport()
    t.enqueue(200, order_json(client_order_id="c1", status="new"))  # get_by_client_id hit
    gk = gatekeeper()
    a = adapter(t)
    a.attach_staging_key(gk.signing_key)
    staged = staged_order(gk)
    order = a.submit(staged)
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
    order = a.submit(staged)
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
        a.submit(staged)
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
    order = a.submit(staged)
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
        a.submit(staged)


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
