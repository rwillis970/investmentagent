"""agent/approval_trigger.py (unattended wiring unit, 2026-08-01, Unit 4):
AnalysisResult -> the four gates -> a signed StagedOrder -> an approval
request. No real API call, no order submission -- StagedOrder is the
furthest this reaches.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from agent.accounts import AccountType
from agent.approval_request_store import ApprovalRequestStore
from agent.approval_trigger import (ApprovalTriggerError,
                                    request_approval_for_analysis)
from agent.audit import AuditLog
from agent.broker.base import AccountSnapshot, Position
from agent.daytrade import DayTradeGuard
from agent.entities import AnalysisResult, OpportunityEvent
from agent.holding import HoldingPolicy, HoldingPolicyRegistry
from agent.ledger import Fill, Ledger
from agent.pipeline import Gatekeeper
from agent.policy import initial_policy
from agent.risk import RiskPolicy

ACCT = "acct-taxable"
T0 = datetime(2026, 8, 1, 15, 0, tzinfo=timezone.utc)   # a real Saturday? check below

# 2026-07-20 is a confirmed real trading Monday elsewhere in this suite;
# reuse the same instant shape for session alignment.
NOW = datetime(2026, 7, 20, 15, 0, tzinfo=timezone.utc)

RISK = RiskPolicy("t", max_position_pct=10.0, max_sector_pct=100.0,
                  min_settled_cash_pct_of_nlv=5.0, min_absolute_settled_cash=10.0)
HOLD = HoldingPolicyRegistry([HoldingPolicy("hp-v1", timedelta(hours=1), timedelta(days=1))])

ANALYSIS = {
    "bull_case": [{"text": "Strong quarter.", "citations": ["abc123"]}],
    "bear_case": [{"text": "Margins compressed.", "citations": ["def456"]}],
    "contradicting_evidence": [], "confidence": 0.7,
}


def gatekeeper(*, live=False):
    return Gatekeeper(account_id=ACCT, account_type=AccountType.TAXABLE,
                      capability_policy=initial_policy(), risk_policy=RISK,
                      day_trade_guard=DayTradeGuard(account_id=ACCT, max_per_5_sessions=3),
                      live=live)


def account_snapshot(*, equity=500.0, settled_cash=500.0):
    return AccountSnapshot(account_id=ACCT, equity=Decimal(str(equity)),
                           cash=Decimal(str(settled_cash)), settled_cash=Decimal(str(settled_cash)),
                           unsettled_cash=Decimal("0"), buying_power=Decimal(str(settled_cash)),
                           multiplier=Decimal("1"), pattern_day_trader=False,
                           day_trade_count=0, fetched_at=NOW)


def ledger(*, opening_cash=500.0):
    return Ledger(account_id=ACCT, opening_settled_cash=Decimal(str(opening_cash)),
                  policy_registry=HOLD, t_plus=1)


def event(*, symbols=("AAPL",)):
    return OpportunityEvent(
        event_id="sec_edgar:AAPL:2026-07-19T09:00:00+00:00", type="FILING",
        source_id="sec_edgar", observed_at=NOW - timedelta(days=1),
        effective_at=NOW - timedelta(days=1), symbols=symbols,
        materiality_score=3.5, score_components={}, threshold_version="v1",
        analysis_status="PENDING_ANALYSIS",
    )


def analysis_result(**over):
    kw = dict(result_id="ar-1", event_id="sec_edgar:AAPL:2026-07-19T09:00:00+00:00",
             symbol="AAPL", model_id="claude-sonnet-5", prompt_version="t4-prompt-v1",
             schema_version="t4-schema-v1", validator_version="t4-validator-v1",
             doc_sha256="a" * 64, cache_hit=False, cost_usd=0.15, confidence=0.7,
             analysis=ANALYSIS, analyzed_at=NOW)
    kw.update(over)
    return AnalysisResult(**kw)


def call(store, tmp_path, *, gk=None, acct_snapshot=None, positions=(), led=None,
         held_qty=None, **over):
    gk = gk or gatekeeper()
    kw = dict(
        event=event(), analysis_result=analysis_result(),
        gatekeeper=gk, ledger=led or ledger(), broker_account=acct_snapshot or account_snapshot(),
        broker_positions=positions, day_trade_guard=gk.day_trade_guard,
        account_type=AccountType.TAXABLE, posture="CASH", price_at_analysis=100.0,
        max_position_pct=10.0, minimum_holding_period=timedelta(hours=1),
        approval_request_store=store, audit_log=AuditLog(),
        max_approval_requests_per_day=4, approval_expiration=timedelta(minutes=30),
        price_band_pct=1.0, estimated_short_term_tax_rate=None,
        estimated_long_term_tax_rate=None, run_id="run-1", now=NOW,
    )
    kw.update(over)
    return request_approval_for_analysis(**kw)


def store(tmp_path, name="approval_request.jsonl"):
    return ApprovalRequestStore(tmp_path / name)


# ---------------------------------------------------------------------- BUY

def test_buy_creates_a_request_sized_to_max_position_pct(tmp_path):
    s = store(tmp_path)
    result = call(s, tmp_path)
    assert result.suppressed_reason is None
    assert result.staged.side == "BUY"
    # requested notional ~= 10% of 500 = 50; qty = 50/100 = 0.5
    assert result.staged.requested_qty == pytest.approx(0.5)
    assert result.request is not None
    assert result.request.proposal_snapshot["symbol"] == "AAPL"
    assert result.request.proposal_snapshot["confidence"] == 0.7


def test_buy_post_trade_reserve_and_concentration_are_computed(tmp_path):
    s = store(tmp_path)
    result = call(s, tmp_path)
    post_trade = result.request.risk_result["post_trade"]
    # authorized notional = qty*price
    notional = result.staged.notional
    expected_reserve_pct = (500.0 - notional) / 500.0 * 100.0
    assert post_trade["reserve_pct_after"] == pytest.approx(expected_reserve_pct)
    assert post_trade["concentration_pct_after"] == pytest.approx(notional / 500.0 * 100.0)
    assert post_trade["earliest_normal_exit_after"] is not None


def test_buy_wash_sale_window_flag_true_after_a_recent_sell(tmp_path):
    s = store(tmp_path)
    led = ledger()
    led.record_fill(Fill(fill_id="f0", account_id=ACCT, symbol="AAPL", side="BUY",
                         qty=Decimal("1"), price=Decimal("90"),
                         filled_at=NOW - timedelta(days=40), lot_id="l0",
                         holding_policy_version="hp-v1"))
    led.record_fill(Fill(fill_id="f1", account_id=ACCT, symbol="AAPL", side="SELL",
                         qty=Decimal("1"), price=Decimal("80"),
                         filled_at=NOW - timedelta(days=10), lot_id="l0"))
    result = call(s, tmp_path, led=led)
    assert result.request.risk_result["tax"]["wash_sale_window"] is True


def test_buy_wash_sale_window_flag_false_with_no_recent_sell(tmp_path):
    s = store(tmp_path)
    result = call(s, tmp_path)
    assert result.request.risk_result["tax"]["wash_sale_window"] is False


def test_buy_request_records_its_own_earmark_equal_to_its_authorized_notional(tmp_path):
    s = store(tmp_path)
    result = call(s, tmp_path)
    assert result.request.earmark == pytest.approx(result.staged.notional)


# --------------------------------------------------------------- earmarking

def test_a_pending_sibling_buy_earmark_reduces_the_second_requests_investable_cash(tmp_path):
    """Items 1-3: a first pending BUY (MSFT) earmarks its own notional; a
    second (AAPL) sees that earmark as `pending_buy_notional` -- both in
    sizing (`agent.risk.risk_constrain`, via `Gatekeeper.stage`) and in its
    own post-trade reserve figure, which must describe the world in which
    BOTH earmarks fill, not just this order's own."""
    s = store(tmp_path)
    first = call(s, tmp_path, event=event(symbols=("MSFT",)))
    assert first.request is not None
    first_notional = first.staged.notional

    second = call(s, tmp_path, event=event(symbols=("AAPL",)))
    assert second.request is not None
    post_trade = second.request.risk_result["post_trade"]

    expected_reserve_pct = (
        (500.0 - second.staged.notional - first_notional) / 500.0 * 100.0
    )
    assert post_trade["reserve_pct_after"] == pytest.approx(expected_reserve_pct)


def test_concentration_pct_after_stays_symbol_specific_not_netted_against_a_sibling(tmp_path):
    s = store(tmp_path)
    call(s, tmp_path, event=event(symbols=("MSFT",)))
    second = call(s, tmp_path, event=event(symbols=("AAPL",)))
    post_trade = second.request.risk_result["post_trade"]
    assert post_trade["concentration_pct_after"] == pytest.approx(
        second.staged.notional / 500.0 * 100.0
    )


def test_sector_exposure_pct_after_nets_out_every_other_outstanding_earmark(tmp_path):
    s = store(tmp_path)
    first = call(s, tmp_path, event=event(symbols=("MSFT",)))
    second = call(s, tmp_path, event=event(symbols=("AAPL",)))
    post_trade = second.request.risk_result["post_trade"]
    expected = (second.staged.notional + first.staged.notional) / 500.0 * 100.0
    assert post_trade["sector_exposure_pct_after"] == pytest.approx(expected)


def test_the_fully_netted_reserve_figure_does_not_depend_on_which_pending_buy_came_first(tmp_path):
    """'Two simultaneous pending BUYs... both cards report the same reserve
    figure' (this unit's prompt): a card is frozen at creation and never
    recomputed once a sibling appears (see agent/approval_request_store.py's
    own module docstring on why sibling invalidation was removed rather
    than kept as a recompute-on-read mechanism), so the FIRST-created card
    in a single run never itself sees the second's earmark. What IS
    order-independent -- and what this test actually proves -- is the fully
    -netted total: whichever of the two symbols happens to be requested
    first, the SECOND (fully-informed) card always reports the identical
    reserve figure, because addition commutes over "my own notional" and
    "the other one's earmark"."""
    # Ordering A: MSFT first, then AAPL.
    s_a = store(tmp_path, name="a.jsonl")
    first_a = call(s_a, tmp_path, event=event(symbols=("MSFT",)))
    second_a = call(s_a, tmp_path, event=event(symbols=("AAPL",)))

    # Ordering B: AAPL first, then MSFT -- same two symbols, swapped order.
    s_b = store(tmp_path, name="b.jsonl")
    first_b = call(s_b, tmp_path, event=event(symbols=("AAPL",)))
    second_b = call(s_b, tmp_path, event=event(symbols=("MSFT",)))

    reserve_a = second_a.request.risk_result["post_trade"]["reserve_pct_after"]
    reserve_b = second_b.request.risk_result["post_trade"]["reserve_pct_after"]
    assert reserve_a == pytest.approx(reserve_b)

    expected = (500.0 - first_a.staged.notional - second_a.staged.notional) / 500.0 * 100.0
    assert reserve_a == pytest.approx(expected)


def test_approval_service_wiring_keeps_an_approved_unconsumed_siblings_earmark_outstanding(tmp_path):
    """Bridge unit, 2026-08-02, item 2 (earmark handoff): passing the new,
    optional `approval_service` parameter through to `outstanding_earmarks`
    means an APPROVED-but-unconsumed sibling's earmark still nets into a
    later request's sizing/post-trade figures -- not just a still-pending
    (undecided) sibling's, which the tests above already cover. Omitting it
    (the default, `None`) preserves this function's exact prior behaviour:
    a decided sibling's earmark is invisible the instant it is decided,
    approved or not."""
    from agent.approval import ApprovalService

    def prep(name):
        # Own store per branch -- otherwise the OTHER branch's own AAPL
        # request (created but never decided, since this test never decides
        # it) would itself linger as an ordinary PENDING sibling and get
        # counted by plain pending() regardless of `service`, confounding
        # the comparison this test is actually about.
        s = store(tmp_path, name=name)
        svc = ApprovalService(expiration=timedelta(minutes=30),
                              min_display=timedelta(seconds=0), max_per_day=4)
        first = call(s, tmp_path, event=event(symbols=("MSFT",)))
        assert first.request is not None
        decide_at = NOW + timedelta(seconds=5)
        s.decide(first.request.request_id, decision="APPROVED", now=decide_at,
                 decided_by="operator")
        svc.approve(token_id=f"tok-{first.request.request_id}",
                   request_id=first.request.request_id, fingerprint="fp",
                   price_at_analysis=100.0, shown_at=first.request.shown_at,
                   now=decide_at)
        return s, svc, first.staged.notional, decide_at

    # WITHOUT approval_service: the now-decided (APPROVED) MSFT sibling's
    # earmark is no longer seen -- pending() already excludes it.
    s1, _svc1, _first_notional1, decide_at1 = prep("without.jsonl")
    without_service = call(s1, tmp_path, event=event(symbols=("AAPL",)), now=decide_at1)
    reserve_without = without_service.request.risk_result["post_trade"]["reserve_pct_after"]
    assert reserve_without == pytest.approx(
        (500.0 - without_service.staged.notional) / 500.0 * 100.0
    )

    # WITH approval_service: the MSFT sibling's still-live token means its
    # earmark is folded back in, exactly as if it were still pending.
    s2, svc2, first_notional2, decide_at2 = prep("with.jsonl")
    with_service = call(s2, tmp_path, event=event(symbols=("AAPL",)), now=decide_at2,
                       approval_service=svc2)
    reserve_with = with_service.request.risk_result["post_trade"]["reserve_pct_after"]
    expected = (500.0 - with_service.staged.notional - first_notional2) / 500.0 * 100.0
    assert reserve_with == pytest.approx(expected)
    assert reserve_with != pytest.approx(reserve_without)   # the wiring must actually change the figure


# -------------------------------------------------------------------- CLOSE

LONG_TERM_OPEN = datetime(2025, 6, 16, 15, 0, tzinfo=timezone.utc)   # a real, confirmed trading day


def held_ledger():
    led = ledger()
    led.record_fill(Fill(fill_id="f0", account_id=ACCT, symbol="AAPL", side="BUY",
                         qty=Decimal("2"), price=Decimal("80"),
                         filled_at=LONG_TERM_OPEN, lot_id="l0",
                         holding_policy_version="hp-v1"))
    return led


def test_close_proposes_full_reconciled_position_qty(tmp_path):
    s = store(tmp_path)
    led = held_ledger()
    positions = (Position(account_id=ACCT, symbol="AAPL", qty=Decimal("2"),
                          avg_price=Decimal("80"), market_value=Decimal("200")),)
    result = call(s, tmp_path, led=led, positions=positions)
    assert result.staged.side == "CLOSE"
    assert result.staged.requested_qty == pytest.approx(2.0)


def test_close_realized_gain_and_long_term_character(tmp_path):
    s = store(tmp_path)
    led = held_ledger()   # opened 400 days ago -- long-term
    positions = (Position(account_id=ACCT, symbol="AAPL", qty=Decimal("2"),
                          avg_price=Decimal("80"), market_value=Decimal("200")),)
    result = call(s, tmp_path, led=led, positions=positions, price_at_analysis=100.0)
    tax = result.request.risk_result["tax"]
    assert tax["character"] == "long_term"
    assert tax["realized_gain"] == pytest.approx(200.0 - 160.0)   # proceeds - cost_basis


def test_close_estimated_tax_is_none_without_a_configured_rate(tmp_path):
    s = store(tmp_path)
    led = held_ledger()
    positions = (Position(account_id=ACCT, symbol="AAPL", qty=Decimal("2"),
                          avg_price=Decimal("80"), market_value=Decimal("200")),)
    result = call(s, tmp_path, led=led, positions=positions)
    assert result.request.risk_result["tax"]["estimated_tax"] is None


def test_close_estimated_tax_uses_configured_long_term_rate(tmp_path):
    s = store(tmp_path)
    led = held_ledger()
    positions = (Position(account_id=ACCT, symbol="AAPL", qty=Decimal("2"),
                          avg_price=Decimal("80"), market_value=Decimal("200")),)
    result = call(s, tmp_path, led=led, positions=positions,
                 estimated_long_term_tax_rate=0.15)
    tax = result.request.risk_result["tax"]
    assert tax["estimated_tax"] == pytest.approx(tax["realized_gain"] * 0.15)


# ------------------------------------- two-tranche mixed character (cleanup unit)
# REVIEW FIX: `_tax_figures` used to sum cost_basis across ALL open lots but
# classify the WHOLE position once, keyed on the OLDEST lot's opened_at --
# so a position built in two tranches (one long-term, one still short-term)
# reported its entire realized gain as long-term, understating the tax due
# on the short-term slice. Fixed by walking lots in the broker's own real
# disposal order (agent.lot_selection.disposal_order / BROKER_FIFO),
# classifying each lot independently, and reporting per-character totals.

SHORT_TERM_OPEN = datetime(2026, 6, 25, 15, 0, tzinfo=timezone.utc)   # a confirmed trading day, ~25d before NOW


def two_tranche_ledger():
    led = ledger()
    led.record_fill(Fill(fill_id="f0", account_id=ACCT, symbol="AAPL", side="BUY",
                         qty=Decimal("2"), price=Decimal("80"),
                         filled_at=LONG_TERM_OPEN, lot_id="l0",
                         holding_policy_version="hp-v1"))
    led.record_fill(Fill(fill_id="f1", account_id=ACCT, symbol="AAPL", side="BUY",
                         qty=Decimal("1"), price=Decimal("90"),
                         filled_at=SHORT_TERM_OPEN, lot_id="l1",
                         holding_policy_version="hp-v1"))
    return led


def test_close_across_two_tranches_reports_mixed_character_and_per_component_totals(tmp_path):
    s = store(tmp_path)
    led = two_tranche_ledger()
    # 3 shares total (2 long-term @ cost 80, 1 short-term @ cost 90), closed
    # at price_at_analysis=100 -> notional=300, allocated proportionally to
    # each lot's own share of the 3 shares disposed: the long-term lot gets
    # 200 of proceeds (2/3 * 300), the short-term lot gets 100 (1/3 * 300).
    positions = (Position(account_id=ACCT, symbol="AAPL", qty=Decimal("3"),
                          avg_price=Decimal("83.33"), market_value=Decimal("300")),)
    result = call(s, tmp_path, led=led, positions=positions, price_at_analysis=100.0,
                 estimated_short_term_tax_rate=0.30, estimated_long_term_tax_rate=0.15)
    assert result.staged.requested_qty == pytest.approx(3.0)
    tax = result.request.risk_result["tax"]
    assert tax["character"] == "mixed"
    # long-term lot: proceeds 200, cost_basis 160 -> gain 40
    assert tax["realized_gain_long_term"] == pytest.approx(40.0)
    # short-term lot: proceeds 100, cost_basis 90 -> gain 10
    assert tax["realized_gain_short_term"] == pytest.approx(10.0)
    assert tax["realized_gain"] == pytest.approx(50.0)
    # each component priced at its OWN rate: 10*0.30 + 40*0.15 = 3.0 + 6.0
    assert tax["estimated_tax"] == pytest.approx(9.0)


def test_close_across_two_tranches_estimated_tax_uses_only_the_configured_component(tmp_path):
    """If only ONE component's rate is configured, the other contributes
    nothing (not a fabricated $0 folded into a single blended rate)."""
    s = store(tmp_path)
    led = two_tranche_ledger()
    positions = (Position(account_id=ACCT, symbol="AAPL", qty=Decimal("3"),
                          avg_price=Decimal("83.33"), market_value=Decimal("300")),)
    result = call(s, tmp_path, led=led, positions=positions, price_at_analysis=100.0,
                 estimated_short_term_tax_rate=0.30, estimated_long_term_tax_rate=None)
    tax = result.request.risk_result["tax"]
    assert tax["estimated_tax"] == pytest.approx(10.0 * 0.30)


def test_a_single_tranche_close_still_reports_a_pure_character_not_mixed(tmp_path):
    """Backward-compatibility check: a position with only ONE lot must
    still report a plain "long_term"/"short_term" character, never
    "mixed" -- mixed requires lots of BOTH characters to actually be
    disposed."""
    s = store(tmp_path)
    led = held_ledger()   # single long-term lot only
    positions = (Position(account_id=ACCT, symbol="AAPL", qty=Decimal("2"),
                          avg_price=Decimal("80"), market_value=Decimal("200")),)
    result = call(s, tmp_path, led=led, positions=positions, price_at_analysis=100.0)
    tax = result.request.risk_result["tax"]
    assert tax["character"] == "long_term"
    assert tax["realized_gain_short_term"] == pytest.approx(0.0)
    assert tax["realized_gain_long_term"] == pytest.approx(40.0)


# --------------------------------------------------------------- rate limit

def test_exceeding_the_daily_cap_suppresses_and_audits_not_creates(tmp_path):
    """The cap counts DECIDED requests only (earmarking unit) -- pre-filling
    it means creating AND deciding 4 requests, not merely creating them."""
    s = store(tmp_path)
    audit = AuditLog()
    # Pre-fill the cap.
    for i in range(4):
        req = s.create(account_id=ACCT, run_id="r", proposal_snapshot={}, risk_result={},
                       price_at_analysis=100.0, price_band_low=99.0, price_band_high=101.0,
                       now=NOW, expiration=timedelta(minutes=30))
        s.decide(req.request_id, decision="APPROVED", now=NOW + timedelta(seconds=5),
                 decided_by="operator")
    result = call(s, tmp_path, audit_log=audit, max_approval_requests_per_day=4)
    assert result.request is None
    assert result.suppressed_reason == "approval_cap"
    actions = [e.action for e in audit.events]
    assert "approval_request_suppressed" in actions


# ------------------------------------------------------------------- gates

def test_a_rejected_gate_suppresses_with_the_gate_named_not_a_raised_error(tmp_path):
    """A zero-sized request (max_position_pct=0) that the risk gate
    authorizes down to zero -- deliberately NOT a cash-caused rejection
    (earmarking unit moved that specific case to its own
    "insufficient_settled_cash" suppression, checked BEFORE this gate is
    ever reached; see the dedicated tests below), so this still exercises
    a genuine `gate:risk:...` rejection surfacing as a suppression, not a
    raised error."""
    s = store(tmp_path)
    audit = AuditLog()
    result = call(s, tmp_path, audit_log=audit, max_position_pct=0.0)
    assert result.request is None
    assert result.suppressed_reason is not None
    assert result.suppressed_reason.startswith("gate:")
    actions = [e.action for e in audit.events]
    assert "approval_request_suppressed" in actions


def test_multi_symbol_event_is_rejected_outright(tmp_path):
    s = store(tmp_path)
    with pytest.raises(ApprovalTriggerError):
        call(s, tmp_path, event=event(symbols=("AAPL", "MSFT")))


# --------------------------------------------------------- insufficient cash

def test_insufficient_settled_cash_suppresses_before_staging_creates_no_request(tmp_path):
    """Earmarking unit item 6: insufficient cash is a SUPPRESSION, checked
    BEFORE `Gatekeeper.stage` is ever called -- no gate is reached, no
    StagedOrder exists. Settled cash (20) is smaller than the required
    reserve (max(500*5%, 10) = 25), so investable cash is 0 and the full
    requested notional (10% of 500 = 50) cannot fit at all."""
    s = store(tmp_path)
    audit = AuditLog()
    result = call(s, tmp_path, audit_log=audit,
                 acct_snapshot=account_snapshot(equity=500.0, settled_cash=20.0))
    assert result.request is None
    assert result.staged is None
    assert result.suppressed_reason == "insufficient_settled_cash"
    row = next(e for e in audit.events if e.action == "approval_request_suppressed")
    assert row.after["reason"] == "insufficient_settled_cash"
    assert row.after["required"] == pytest.approx(50.0)
    assert row.after["available"] == pytest.approx(0.0)


def test_insufficient_settled_cash_accounts_for_other_pending_earmarks_too(tmp_path):
    """A request that WOULD fit against gross settled cash alone can still
    be suppressed once another pending BUY's earmark is netted out first."""
    s = store(tmp_path)
    # settled_cash=100: reserve=25, so in isolation 50 (the requested
    # notional) would fit (100-25=75 available). A pre-existing pending
    # earmark of 60 leaves only 15 available, which does not.
    s.create(account_id=ACCT, run_id="r", proposal_snapshot={}, risk_result={},
            price_at_analysis=100.0, price_band_low=99.0, price_band_high=101.0,
            now=NOW, expiration=timedelta(minutes=30), earmark=60.0)
    result = call(s, tmp_path,
                 acct_snapshot=account_snapshot(equity=500.0, settled_cash=100.0))
    assert result.suppressed_reason == "insufficient_settled_cash"


