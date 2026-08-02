"""AnalysisResult -> the four capability gates + risk/holding/day-trade ->
a signed StagedOrder -> an approval request (§9, §10, §3.4; unattended
wiring unit, 2026-08-01, Unit 4). Nothing in this codebase built this path
before this unit: `agent.entities.ApprovalRequest` (Day-1) had no
constructor anywhere, and `agent.approval.ApprovalService` mints/expires/
sweeps/consumes tokens but never created a REQUEST from an analysis.

SIZING IS NOT THE MODEL'S JOB (invariant #6). `agent.analysis_output.
AnalysisOutput` deliberately carries no qty/side/target-weight -- bull case,
bear case, contradicting evidence, confidence, nothing else (confirmed
directly: no code review is needed, the schema simply has no such field).
This module proposes a REQUESTED size using only this codebase's OWN
configured policy, never anything extracted or inferred by the model:

  - BUY (the event's symbol is not currently held): request the account's
    own configured `max_position_pct` of NLV -- the same ceiling `agent.risk.
    risk_constrain` would clip an oversized request to anyway. This is a
    request, not an authorization: `Gatekeeper.stage` (all four gates, §5.1)
    runs the real, existing target-weight-vector risk_constrain and RESIZES
    down to whatever current exposure/sector/reserve actually allow, exactly
    the same as any other BUY through this codebase's one gated path
    (invariant #2). Confidence is NEVER used to scale size -- it is
    display-only on the resulting card, the same as bull_case/bear_case.

  - SELL/CLOSE (the event's symbol IS currently held -- `agent.materiality_
    cycle.run_materiality_cycle`'s own `held_symbols`/`side="SELL"` logic
    already decided this before the event ever reached T4): propose a
    `side="CLOSE"`, whose quantity `Gatekeeper.stage` resolves from
    `broker_position_qty` -- the reconciled broker read, never a
    caller-supplied belief (§4.1's own CLOSE semantics, unchanged here).
    No partial-exit sizing logic is invented in this module.

NO SECTOR CLASSIFICATION SOURCE EXISTS (disclosed, pre-existing gap --
`agent.materiality_cycle`'s own module docstring already names the same
absence for `sector_ret`). `sectors={}` is passed to `Gatekeeper.stage`
exactly as every other real call site in this codebase does today --
`risk_constrain`'s own sector-cap step is consequently a no-op (every
symbol falls into one undifferentiated "UNKNOWN" bucket). The post-trade
"sector exposure" figure on the card is honestly computed AS the sum of
every held symbol's post-trade weight (the whole book, since nothing
distinguishes one sector from another) -- not a fabricated per-GICS-sector
number. See `_post_trade_state` below.

RATE LIMIT (§3.4, Unit 4 item 1), RE-CHECKED HERE, NOT TRUSTED FROM
SCREEN TIME. `agent.materiality.screen`'s own `approvals_ok` gate already
checks `approvals_today < max_approval_requests_per_day` -- but that count
is taken AT SCREEN TIME, and T4 analysis (a real model call, possibly
cache-miss) can complete well after that screen ran, during which another
event's approval request may have consumed the day's remaining cap. This
module re-derives `requests_today` from the durable `agent.
approval_request_store.ApprovalRequestStore` itself, immediately before
creating a request -- the same "recheck against the real, current resource
at the point of consumption, not a stale precondition" pattern this unit
already applied to `agent.cost.CostLedger.analyses_today` in Unit 2/3. If
creating this request would exceed the cap, NO request is created and an
`agent.audit.AuditLog` row records the suppression by name
(`approval_request_suppressed`, reason `"approval_cap"`) -- never a silent
drop.

POST-TRADE STATE (§10, Unit 4 item 2): computed from `broker_account`/
`broker_positions`/`ledger` PLUS the staged order -- never from current
state alone. See `_post_trade_state` below for all five figures.

TAX FIGURES (§10, Unit 4 item 5): `agent.tax.classify` for a SELL/CLOSE
(character, realised gain, wash-sale flag); a wash-sale-WINDOW flag for a
BUY. `estimated_tax` is `None` unless the operator has configured a real
marginal rate (`agent.config.Config.estimated_short_term_tax_rate`/
`estimated_long_term_tax_rate`) -- see that field's own docstring for why
this codebase refuses to guess one.

A `Rejected` FROM `Gatekeeper.stage` (any of the four gates, holding, or
day-trade) IS A NORMAL, AUDITED OUTCOME, NOT AN ERROR THAT HALTS THE
CYCLE -- named in the audit log (`approval_request_suppressed`, reason
`"gate:<gate>:<reason>"`), same posture as the rate-limit suppression
above: a screened, materially-worthy event that the risk/holding/day-trade
gates refuse is not a bug, it is exactly those gates doing their job one
step earlier than a human ever sees a card for it.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from . import market_calendar
from .accounts import AccountType
from .approval_request_store import ApprovalRequestStore
from .audit import AuditLog
from .broker.base import AccountSnapshot, Position
from .daytrade import DayTradeGuard
from .entities import AnalysisResult, ApprovalRequest, OpportunityEvent
from .ledger import Ledger
from .pipeline import Gatekeeper, Rejected, StagedOrder
from .risk import PortfolioState
from .tax import TaxCharacter, classify

WASH_SALE_WINDOW_DAYS = 30
LONG_TERM_THRESHOLD_DAYS = 365


class ApprovalTriggerError(Exception):
    pass


@dataclass(frozen=True)
class ApprovalTriggerResult:
    request: ApprovalRequest | None
    staged: StagedOrder | None
    suppressed_reason: str | None = None


def _same_session(a: datetime, b: datetime) -> bool:
    return market_calendar.session_for_instant(a) == market_calendar.session_for_instant(b)


def _post_trade_state(*, symbol: str, side: str, staged: StagedOrder,
                      broker_account: AccountSnapshot,
                      broker_positions: tuple[Position, ...],
                      minimum_holding_period: timedelta,
                      day_trade_guard: DayTradeGuard, opens_day_trade: bool,
                      now: datetime) -> dict:
    """The five §10 post-trade figures -- what will be true AFTER the fill,
    computed from broker-reconciled state plus the proposed order, never
    from current state alone."""
    nlv = float(broker_account.equity)
    settled_cash = float(broker_account.settled_cash)
    signed_notional = staged.notional if side == "BUY" else -staged.notional

    post_trade_settled_cash = settled_cash - staged.notional if side == "BUY" \
        else settled_cash + staged.notional
    reserve_pct_after = (post_trade_settled_cash / nlv * 100.0) if nlv else 0.0

    by_symbol_notional = {p.symbol: float(p.market_value) for p in broker_positions}
    current_notional = by_symbol_notional.get(symbol, 0.0)
    post_trade_symbol_notional = current_notional + signed_notional
    concentration_pct_after = (post_trade_symbol_notional / nlv * 100.0) if nlv else 0.0

    # No sector-classification source exists (module docstring) -- every
    # symbol is one undifferentiated bucket, so "sector exposure" is
    # honestly the whole post-trade book, not a fabricated per-sector split.
    post_trade_total_notional = sum(
        (v + signed_notional if s == symbol else v) for s, v in by_symbol_notional.items()
    )
    if symbol not in by_symbol_notional:
        post_trade_total_notional += signed_notional
    sector_exposure_pct_after = (post_trade_total_notional / nlv * 100.0) if nlv else 0.0

    earliest_normal_exit_after = (
        (now + minimum_holding_period).isoformat() if side == "BUY" else None
    )

    session = market_calendar.session_for_instant(now)
    day_trade_count_after = day_trade_guard.count(session) + (1 if opens_day_trade else 0)

    return {
        "reserve_pct_after": reserve_pct_after,
        "concentration_pct_after": concentration_pct_after,
        "sector_exposure_pct_after": sector_exposure_pct_after,
        "earliest_normal_exit_after": earliest_normal_exit_after,
        "day_trade_count_after": day_trade_count_after,
    }


def _tax_figures(*, symbol: str, side: str, staged: StagedOrder, ledger: Ledger,
                 account_type: AccountType, now: datetime,
                 estimated_short_term_tax_rate: float | None,
                 estimated_long_term_tax_rate: float | None) -> dict:
    """§10 Unit 4 item 5. For a SELL/CLOSE: character, realised gain, an
    estimated dollar tax (only if the operator configured a real rate --
    see agent.config.Config's own docstring for why a guess is refused),
    and the date long-term treatment would have begun. For a BUY: whether
    it lands inside a wash-sale window (a CONSERVATIVE flag -- "this symbol
    was sold within the last 30 days", not a certified determination that
    the specific prior sale was a loss; the operator is expected to check
    §4.5's cooldown/wash-sale interaction before relying on it)."""
    fills = ledger.fills
    if side in ("SELL", "CLOSE"):
        open_lots = [l for l in ledger.lots() if l.symbol == symbol and l.is_open()]
        if not open_lots:
            return {"character": None, "realized_gain": None, "estimated_tax": None,
                   "wash_sale_flag": None, "long_term_treatment_begins": None}
        oldest = min(open_lots, key=lambda l: l.opened_at)
        total_cost_basis = float(sum(l.cost_basis for l in open_lots))
        repurchased_within_window = any(
            f.symbol == symbol and f.side.upper() == "BUY"
            and timedelta(0) <= (now - f.filled_at) <= timedelta(days=WASH_SALE_WINDOW_DAYS)
            for f in fills
        )
        result = classify(
            account_type=account_type, opened_at=oldest.opened_at, closed_at=now,
            proceeds=staged.notional, cost_basis=total_cost_basis,
            repurchased_within_window=repurchased_within_window,
        )
        rate = (estimated_long_term_tax_rate if result.character == TaxCharacter.LONG_TERM
               else estimated_short_term_tax_rate)
        estimated_tax = (result.realized_gain * rate
                        if result.realized_gain is not None and result.realized_gain > 0
                        and rate is not None else None)
        long_term_begins = (oldest.opened_at + timedelta(days=LONG_TERM_THRESHOLD_DAYS))
        return {
            "character": result.character.value if result.character else None,
            "realized_gain": result.realized_gain, "estimated_tax": estimated_tax,
            "wash_sale_flag": result.wash_sale_flag,
            "long_term_treatment_begins": long_term_begins.isoformat(),
        }

    # BUY: wash-sale-window check only.
    in_window = any(
        f.symbol == symbol and f.side.upper() == "SELL"
        and timedelta(0) <= (now - f.filled_at) <= timedelta(days=WASH_SALE_WINDOW_DAYS)
        for f in fills
    )
    return {"wash_sale_window": in_window}


def request_approval_for_analysis(
    event: OpportunityEvent, analysis_result: AnalysisResult, *,
    gatekeeper: Gatekeeper, ledger: Ledger, broker_account: AccountSnapshot,
    broker_positions: tuple[Position, ...], day_trade_guard: DayTradeGuard,
    account_type: AccountType, posture: str, price_at_analysis: float,
    max_position_pct: float, minimum_holding_period: timedelta,
    approval_request_store: ApprovalRequestStore, audit_log: AuditLog,
    max_approval_requests_per_day: int, approval_expiration: timedelta,
    price_band_pct: float, estimated_short_term_tax_rate: float | None,
    estimated_long_term_tax_rate: float | None, run_id: str, now: datetime,
) -> ApprovalTriggerResult:
    """See module docstring. Raises nothing on a normal suppression (rate
    cap or a refused gate) -- both come back as `ApprovalTriggerResult.
    suppressed_reason`, audited, never silently dropped and never treated
    as a cycle-halting error."""
    if len(event.symbols) != 1:
        raise ApprovalTriggerError(
            f"event {event.event_id!r} must carry exactly one symbol"
        )
    symbol = event.symbols[0]
    held_qty = float(ledger.positions().get(symbol, 0) or 0)
    side = "CLOSE" if held_qty > 0 else "BUY"

    nlv = float(broker_account.equity)
    current_weights = {
        p.symbol: (float(p.market_value) / nlv if nlv else 0.0) for p in broker_positions
    }
    portfolio = PortfolioState(
        account_id=gatekeeper.account_id, nlv=nlv,
        settled_cash=float(broker_account.settled_cash),
        unsettled_cash=float(broker_account.unsettled_cash),
    )
    opens_day_trade = any(
        f.symbol == symbol and _same_session(f.filled_at, now) for f in ledger.fills
    )

    qty = None
    broker_position_qty = None
    if side == "BUY":
        requested_notional = (max_position_pct / 100.0) * nlv
        qty = requested_notional / price_at_analysis if price_at_analysis else 0.0
    else:
        broker_position_qty = held_qty

    try:
        staged = gatekeeper.stage(
            client_order_id=f"t4-{analysis_result.result_id}", symbol=symbol, side=side,
            order_type="LIMIT", time_in_force="DAY", portfolio=portfolio, now=now,
            posture=posture, qty=qty, price=price_at_analysis,
            limit_price=price_at_analysis, asset_class="US_EQUITY",
            funding="SETTLED_CASH", session="REGULAR", lots=ledger.lots(),
            opens_day_trade=opens_day_trade, sectors={}, current_weights=current_weights,
            broker_position_qty=broker_position_qty, lot_id=None,
        )
    except Rejected as exc:
        reason = f"gate:{exc.gate}:{exc.reason}"
        audit_log.append(
            actor="system", action="approval_request_suppressed",
            object_type="opportunity_event", object_id=event.event_id,
            after={"reason": reason, "symbol": symbol, "side": side}, timestamp=now,
        )
        return ApprovalTriggerResult(request=None, staged=None, suppressed_reason=reason)

    requests_today = approval_request_store.count_created_on(now.date())
    if requests_today >= max_approval_requests_per_day:
        audit_log.append(
            actor="system", action="approval_request_suppressed",
            object_type="opportunity_event", object_id=event.event_id,
            after={"reason": "approval_cap", "symbol": symbol, "side": side,
                  "requests_today": requests_today,
                  "max_approval_requests_per_day": max_approval_requests_per_day},
            timestamp=now,
        )
        return ApprovalTriggerResult(request=None, staged=staged,
                                     suppressed_reason="approval_cap")

    post_trade = _post_trade_state(
        symbol=symbol, side=side, staged=staged, broker_account=broker_account,
        broker_positions=broker_positions, minimum_holding_period=minimum_holding_period,
        day_trade_guard=day_trade_guard, opens_day_trade=opens_day_trade, now=now,
    )
    tax = _tax_figures(
        symbol=symbol, side=side, staged=staged, ledger=ledger, account_type=account_type,
        now=now, estimated_short_term_tax_rate=estimated_short_term_tax_rate,
        estimated_long_term_tax_rate=estimated_long_term_tax_rate,
    )

    proposal_snapshot = {
        "event_id": event.event_id, "symbol": symbol, "side": side,
        "requested_qty": staged.requested_qty, "authorized_qty": staged.authorized_qty,
        "order_type": staged.order_type, "time_in_force": staged.time_in_force,
        "limit_price": staged.limit_price, "lot_id": staged.lot_id,
        "confidence": analysis_result.confidence, "analysis": analysis_result.analysis,
        "model_id": analysis_result.model_id, "doc_sha256": analysis_result.doc_sha256,
        "analyzed_at": analysis_result.analyzed_at.isoformat(),
    }
    risk_result = {
        "gates_passed": list(staged.gates_passed), "binding": list(staged.binding),
        "post_trade": post_trade, "tax": tax,
    }
    band_pct = price_band_pct / 100.0
    request = approval_request_store.create(
        account_id=gatekeeper.account_id, run_id=run_id,
        proposal_snapshot=proposal_snapshot, risk_result=risk_result,
        price_at_analysis=price_at_analysis,
        price_band_low=price_at_analysis * (1 - band_pct),
        price_band_high=price_at_analysis * (1 + band_pct),
        now=now, expiration=approval_expiration,
    )
    return ApprovalTriggerResult(request=request, staged=staged, suppressed_reason=None)
