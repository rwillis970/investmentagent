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
drop. `requests_today` is `ApprovalRequestStore.count_decided_on`
(earmarking unit, 2026-08-02; renamed from `count_created_on` -- see that
store's own module docstring): the cap counts DECIDED requests only, not
every request ever created, since a card nobody acted on spent none of the
operator attention the cap protects.

EARMARKING (§6.1, §10; earmarking unit, 2026-08-02) -- CLOSING THE GAP TWO
PENDING BUYS USED TO PAPER OVER WITH SIBLING INVALIDATION. Before this
unit, a pending BUY request consumed no accounted-for cash at all until it
was actually submitted -- so two pending requests against the same account
were each priced (sizing AND post-trade figures) as though the other did
not exist, and `agent.approval_request_store.ApprovalRequestStore.decide`
covered for that by invalidating every other pending sibling the instant
one was approved. `portfolio.pending_buy_notional` (`agent.risk.
PortfolioState`, documented in Change Request §6.1's own reserve-semantics
formula from the very start, but never actually set by any caller in this
codebase until this unit -- confirmed by grep before writing this) is now
set from `approval_request_store.outstanding_earmarks(account_id, now)` --
the sum of every OTHER account's pending BUY request's own earmark, BEFORE
`Gatekeeper.stage` sizes this one. `agent.risk.risk_constrain`'s existing,
UNCHANGED reserve-scale step already subtracts `pending_buy_notional` from
`investable_cash` (`agent/risk.py:72`) -- this was the actual defect: the
field existed and was already wired into the arithmetic, nothing upstream
of it ever populated it. No parallel sizing mechanism is added; this is
the one existing, shared `risk_constrain` path picking up a real number
instead of its own default of `0.0`.

EARMARK HANDOFF (bridge unit, 2026-08-02, Prompt 3) -- `approval_service`,
threaded through as an OPTIONAL parameter (default `None`, so every
pre-existing caller/test keeps its exact prior behaviour), is passed
straight to `outstanding_earmarks(..., service=approval_service)` so an
APPROVED-but-unconsumed sibling's earmark (its token minted by `agent.
approval_bridge.mint_approval_token`, not yet spent/expired/swept) is
folded into `pending_buy_notional` here too, not just a still-undecided
sibling's. Today, no real caller in this codebase passes a real
`ApprovalService` here yet -- there is no operator-facing decision surface
built anywhere in this codebase that would construct one and call this
function with it (see `agent.approval_bridge`'s own module docstring) --
so in practice `approval_service` is `None` at every current call site and
this parameter is presently inert; it exists so the day that surface is
built, the correct, already-tested handoff activates with a one-line
change at that surface, not a second re-derivation of this same
arithmetic.

INSUFFICIENT SETTLED CASH (Unit item 6, earmarking unit) IS A SUPPRESSION,
NOT A GATE REJECTION -- checked BEFORE `Gatekeeper.stage` is ever called,
for BUY only, by comparing the FULL requested notional (before any
resize) against `agent.risk.investable_cash(portfolio, risk_policy)` --
the same shared function `risk_constrain`'s own reserve-scale step calls
internally, not a second, competing calculation. If the requested notional
will not fit, NO request is created, `Gatekeeper.stage` is never even
called (capability/holding/day-trade gates are moot if there is no cash
for the trade at all), and the existing `approval_request_suppressed`
audit row is written with reason `"insufficient_settled_cash"` and the
`required`/`available` dollar amounts. Unlike a generic gate rejection
(`gate:risk:...`, still marked handled -- a real risk/holding/day-trade
refusal IS a fact about the trade), this suppression is deliberately NOT
marked handled in `agent.opportunity_event_tracker.OpportunityEventTracker`
-- today's cash says nothing about the document, exactly the same
reasoning `agent.analysis.BudgetExceeded` already gets (see agent/
pipeline_stage.py's own module docstring and `_analyze_and_request`).

POST-TRADE STATE (§10, Unit 4 item 2, extended earmarking unit item 3):
computed from `broker_account`/`broker_positions`/`ledger` PLUS the staged
order -- never from current state alone. `reserve_pct_after` and
`sector_exposure_pct_after` (the book-wide figures) additionally net out
`portfolio.pending_buy_notional` -- every OTHER pending BUY's earmark --
so they describe the world in which every outstanding earmark fills, not
just this one order. `concentration_pct_after` stays symbol-specific and
is deliberately NOT netted against other earmarks (a sibling earmark
belongs, in the common case, to a different symbol; see `_post_trade_state`
below). See `_post_trade_state` below for all five figures.

TAX FIGURES (§10, Unit 4 item 5): `agent.tax.classify` for a SELL/CLOSE
(character, realised gain, wash-sale flag); a wash-sale-WINDOW flag for a
BUY. `estimated_tax` is `None` unless the operator has configured a real
marginal rate (`agent.config.Config.estimated_short_term_tax_rate`/
`estimated_long_term_tax_rate`) -- see that field's own docstring for why
this codebase refuses to guess one.

PER-LOT CLASSIFICATION, NOT ONE CLASSIFICATION FOR THE WHOLE POSITION
(cleanup unit, review round 3, fixing a defect this same module introduced).
A CLOSE disposes of every open lot for the symbol, and those lots do not
all necessarily share one holding-period character: a position built in two
tranches -- one opened 400 days ago, one 30 -- has ONE short-term lot and
one long-term lot. The original version of this function summed
`cost_basis` across ALL open lots but read `character`/`realized_gain` from
`agent.tax.classify` called ONCE, keyed on the OLDEST lot's `opened_at` --
so the entire realized gain, including the portion attributable to the
30-day-old lot, was reported as LONG_TERM. That understates the tax due on
a figure an operator reads before approving (short-term gains are usually
taxed at a higher marginal rate). Fixed by walking `open_lots` in the SAME
order the broker will actually dispose of them --
`agent.lot_selection.disposal_order` against `ALPACA_DEFAULT_POLICY`, the
one confirmed real disposal method this codebase has (see that module's
own docstring for why a second, invented ordering is refused) -- allocating
proceeds to each lot in proportion to its own share of the total qty
disposed, classifying each lot independently via `agent.tax.classify`, and
summing into two separate totals: `realized_gain_short_term`/
`realized_gain_long_term`. `character` is `"mixed"` when lots of BOTH
characters were actually disposed (not merely when both dollar totals
happen to be nonzero, which would misclassify an exact-break-even lot);
`"short_term"`/`"long_term"` when only one character was disposed;
`realized_gain` (kept, for backward compatibility with the card's single
headline figure) is the sum of both components. `estimated_tax` is now the
SUM of each component priced at ITS OWN configured rate -- a short-term
gain at `estimated_short_term_tax_rate`, a long-term gain at
`estimated_long_term_tax_rate` -- rather than one rate applied to the whole
blended total; a component contributes nothing (not a fabricated $0) when
its own rate is unconfigured, and the whole figure is `None` only when
NEITHER component has a configured rate or a taxable gain. Disposal order
does not change WHICH lots are disposed for a full CLOSE (all of them are)
-- it only decides which lot's own cost basis/holding period is charged
against which slice of the proceeds, which is exactly what determines each
lot's own character and gain.

A `Rejected` FROM `Gatekeeper.stage` (any of the four gates, holding, or
day-trade) IS A NORMAL, AUDITED OUTCOME, NOT AN ERROR THAT HALTS THE
CYCLE -- named in the audit log (`approval_request_suppressed`, reason
`"gate:<gate>:<reason>"`), same posture as the rate-limit suppression
above: a screened, materially-worthy event that the risk/holding/day-trade
gates refuse is not a bug, it is exactly those gates doing their job one
step earlier than a human ever sees a card for it.

PERSIST THE STAGED ORDER ITSELF, NOT JUST THE SCALARS AN OPERATOR READS
(Unit 1, 2026-08-09). Before this unit, `proposal_snapshot` carried only
the handful of scalar fields a human reads off the card (symbol, side,
authorized_qty, order_type, time_in_force, limit_price, lot_id, plus the
analysis metadata) -- nothing durable held the actual `StagedOrder`
`Gatekeeper.stage` produced. Re-deriving one later by calling `stage()` a
second time is refused by design, not merely unbuilt: portfolio state
(cash, positions, other pending earmarks) moves between staging and any
later verification, so a re-staged order can legitimately differ from the
one an operator actually saw and approved -- verifying against a re-staged
order would silently substitute a DIFFERENT order for the one approved.
`_encode_staged_order`/`staged_order_from_snapshot` serialize/reconstruct
every one of `StagedOrder`'s fields (including `gates_passed`/`binding`,
stored as lists since JSON has no tuple) into/from a new top-level
`proposal_snapshot["staged_order"]` key -- every pre-existing top-level
scalar key is left exactly as it was, so `agent.approval_bridge.
mint_approval_token` and `agent.dashboard_state` (both of which only ever
read named top-level scalar keys) need no changes. A `proposal_snapshot`
written before this unit shipped has no `"staged_order"` key at all;
`staged_order_from_snapshot` raises `MissingStagedOrder` for it -- fails
closed, never falls back to re-staging.

`StagedOrder.signature` is persisted verbatim as part of this, but a
signature checked with a DIFFERENT `Gatekeeper.signing_key` than the one
that produced it will never verify: `signing_key` defaults to a fresh
random value per `Gatekeeper` instance (`field(default_factory=lambda:
secrets.token_bytes(32))`, see `agent.pipeline`'s own docstring). At the
time this unit shipped, `scripts/run_agent.py` always took that default,
and nothing in this codebase persisted the key anywhere -- so a later,
separate process (an operator-invoked submit CLI, for instance) held a
DIFFERENT signing key and could never reproduce the original HMAC. This
paragraph originally concluded that whatever verifies a persisted
`StagedOrder` later would have to compare its business-logic fields
against freshly-gathered state rather than re-check `.signature` at all.

SUPERSEDED (follow-up unit, 2026-08-09): that conclusion no longer holds.
`scripts/run_agent.py` now resolves `signing_key` from a durable secret
(`agent.secrets_provider.SecretsProvider.resolve`) instead of the random
default, so two separately-constructed `Gatekeeper` instances that
resolved the same secret produce and verify the SAME signature.
`agent.approval_execution.execute_approved_request` now DOES re-check
`.signature` against the caller's `gatekeeper.signing_key`, and refuses
outright (`StagingSignatureInvalid`, a hard stop -- no fallback) rather
than silently trusting an unverified order's fields. The real, still-open
consequence is at the CUTOVER moment: any `proposal_snapshot["staged_order"]`
signed before the durable key was provisioned carries a signature that can
never verify against it, by construction -- see `agent.approval_execution`'s
own module docstring for the full reasoning and the operator's remedy.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from . import market_calendar
from .accounts import AccountType
from .approval import ApprovalService
from .approval_request_store import ApprovalRequestStore
from .audit import AuditLog
from .broker.base import AccountSnapshot, Position
from .daytrade import DayTradeGuard
from .entities import AnalysisResult, ApprovalRequest, OpportunityEvent
from .ledger import Ledger
from .lot_selection import ALPACA_DEFAULT_POLICY, disposal_order
from .pipeline import Gatekeeper, Rejected, StagedOrder
from .risk import PortfolioState, investable_cash
from .tax import TaxCharacter, classify

WASH_SALE_WINDOW_DAYS = 30
LONG_TERM_THRESHOLD_DAYS = 365


class ApprovalTriggerError(Exception):
    pass


class MissingStagedOrder(ApprovalTriggerError):
    """Raised by `staged_order_from_snapshot` when `proposal_snapshot` has
    no `"staged_order"` key -- a request created before Unit 1 (2026-08-09)
    shipped. There is deliberately no fallback to re-staging: portfolio
    state has moved since the original request was created, so a re-staged
    order can legitimately differ from the one an operator actually saw.
    Silently substituting a re-derived order for a missing persisted one
    would defeat the entire point of persisting it -- this fails closed
    instead."""


_STAGED_ORDER_LIST_FIELDS = ("gates_passed", "binding")
_STAGED_ORDER_FIELDS = (
    "account_id", "client_order_id", "symbol", "side", "requested_qty",
    "authorized_qty", "order_type", "time_in_force", "limit_price",
    "asset_class", "funding", "session", "requested_notional", "notional",
    "gates_passed", "binding", "signature", "lot_id",
)


def _encode_staged_order(staged: StagedOrder) -> dict:
    """Every `StagedOrder` field, verbatim -- enough to reconstruct the
    EXACT order `Gatekeeper.stage` produced, not just the seven fields
    `agent.approval.verify_modification_within_bounds` inspects. Tuple
    fields (`gates_passed`, `binding`) become lists; JSON has no tuple."""
    encoded = {name: getattr(staged, name) for name in _STAGED_ORDER_FIELDS}
    for name in _STAGED_ORDER_LIST_FIELDS:
        encoded[name] = list(encoded[name])
    return encoded


def staged_order_from_snapshot(proposal_snapshot: dict) -> StagedOrder:
    """Reconstructs the persisted `StagedOrder` from a request's
    `proposal_snapshot`. Raises `MissingStagedOrder` -- never re-stages --
    when the key is absent (see that exception's own docstring)."""
    encoded = proposal_snapshot.get("staged_order")
    if encoded is None:
        raise MissingStagedOrder(
            "proposal_snapshot has no 'staged_order' key -- this request "
            "predates Unit 1 (2026-08-09) and cannot be reconstructed; "
            "re-staging is refused by design, not merely unbuilt"
        )
    fields = dict(encoded)
    for name in _STAGED_ORDER_LIST_FIELDS:
        fields[name] = tuple(fields[name])
    return StagedOrder(**fields)


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
                      now: datetime, pending_buy_notional: float = 0.0) -> dict:
    """The five §10 post-trade figures -- what will be true AFTER the fill,
    computed from broker-reconciled state plus the proposed order, never
    from current state alone.

    `pending_buy_notional` (earmarking unit, 2026-08-02): the sum of every
    OTHER pending BUY request's own earmark for this account (`agent.
    approval_request_store.ApprovalRequestStore.outstanding_earmarks`,
    excluding this order since it is not yet a stored request when this
    runs) -- the SAME value already passed to `agent.risk.PortfolioState.
    pending_buy_notional` for sizing. `reserve_pct_after`/
    `sector_exposure_pct_after` net it out too, so both describe the world
    in which every outstanding earmark fills, not just this one order --
    two simultaneous pending BUYs' cards therefore agree on the total
    reserve consumed once both are accounted for, regardless of which one
    is "this" order and which is "the other" (see this unit's own test and
    report). `concentration_pct_after` is deliberately NOT netted here: it
    is symbol-specific, and a sibling earmark belongs, in the common case,
    to a different symbol -- netting a same-symbol sibling's earmark into
    concentration is a real, narrower gap this unit does not close (see
    this unit's own report)."""
    nlv = float(broker_account.equity)
    settled_cash = float(broker_account.settled_cash)
    signed_notional = staged.notional if side == "BUY" else -staged.notional

    # Cash moves OPPOSITE to `signed_notional`'s own sign convention: a BUY
    # (signed_notional > 0, since it INCREASES this symbol's exposure)
    # CONSUMES cash; a SELL/CLOSE (signed_notional < 0) FREES it. Every
    # OTHER outstanding pending BUY's earmark also consumes cash regardless
    # of what side THIS order is.
    post_trade_settled_cash = settled_cash - signed_notional - pending_buy_notional
    reserve_pct_after = (post_trade_settled_cash / nlv * 100.0) if nlv else 0.0

    by_symbol_notional = {p.symbol: float(p.market_value) for p in broker_positions}
    current_notional = by_symbol_notional.get(symbol, 0.0)
    post_trade_symbol_notional = current_notional + signed_notional
    concentration_pct_after = (post_trade_symbol_notional / nlv * 100.0) if nlv else 0.0

    # No sector-classification source exists (module docstring) -- every
    # symbol is one undifferentiated bucket, so "sector exposure" is
    # honestly the whole post-trade book, not a fabricated per-sector split.
    # Every OTHER outstanding earmark also lands somewhere in the book once
    # it fills, so it is added here too (see this function's own docstring).
    post_trade_total_notional = sum(
        (v + signed_notional if s == symbol else v) for s, v in by_symbol_notional.items()
    )
    if symbol not in by_symbol_notional:
        post_trade_total_notional += signed_notional
    post_trade_total_notional += pending_buy_notional
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
    """§10 Unit 4 item 5. For a SELL/CLOSE: per-character realised gain
    totals, an estimated dollar tax (only for whichever component(s) the
    operator configured a real rate for -- see agent.config.Config's own
    docstring for why a guess is refused), and the date long-term treatment
    would begin for any lot not already there. For a BUY: whether it lands
    inside a wash-sale window (a CONSERVATIVE flag -- "this symbol was sold
    within the last 30 days", not a certified determination that the
    specific prior sale was a loss; the operator is expected to check
    §4.5's cooldown/wash-sale interaction before relying on it).

    See module docstring's PER-LOT CLASSIFICATION section for why this
    walks `open_lots` individually (in the broker's own real disposal
    order, `agent.lot_selection.disposal_order`) rather than classifying
    the whole position once against its oldest lot."""
    fills = ledger.fills
    if side in ("SELL", "CLOSE"):
        open_lots = [l for l in ledger.lots() if l.symbol == symbol and l.is_open()]
        if not open_lots:
            return {"character": None, "realized_gain": None,
                   "realized_gain_short_term": None, "realized_gain_long_term": None,
                   "estimated_tax": None, "wash_sale_flag": None,
                   "long_term_treatment_begins": None}

        if account_type.is_retirement:
            # No realized gain/loss, no wash-sale rule, regardless of lot
            # mix -- `agent.tax.classify`'s own retirement branch, applied
            # ONCE here rather than once per lot: every lot would return
            # the identical NOT_APPLICABLE/None/False triple anyway.
            return {"character": TaxCharacter.NOT_APPLICABLE.value, "realized_gain": None,
                   "realized_gain_short_term": None, "realized_gain_long_term": None,
                   "estimated_tax": None, "wash_sale_flag": False,
                   "long_term_treatment_begins": None}

        ordered = disposal_order(ALPACA_DEFAULT_POLICY, open_lots)
        total_qty = float(sum(l.qty for l in ordered))
        repurchased_within_window = any(
            f.symbol == symbol and f.side.upper() == "BUY"
            and timedelta(0) <= (now - f.filled_at) <= timedelta(days=WASH_SALE_WINDOW_DAYS)
            for f in fills
        )

        realized_gain_short_term = 0.0
        realized_gain_long_term = 0.0
        wash_sale_flag = False
        short_term_seen = False
        long_term_seen = False
        latest_short_term_opened_at: datetime | None = None
        for lot in ordered:
            # Proceeds allocated to THIS lot in proportion to its own share
            # of the total qty disposed -- disposal order does not change
            # WHICH lots are disposed for a full CLOSE (all of them are),
            # only which lot's own cost basis/holding period is charged
            # against which slice of the proceeds.
            lot_qty = float(lot.qty)
            proceeds_share = (staged.notional * (lot_qty / total_qty)) if total_qty else 0.0
            result = classify(
                account_type=account_type, opened_at=lot.opened_at, closed_at=now,
                proceeds=proceeds_share, cost_basis=float(lot.cost_basis),
                repurchased_within_window=repurchased_within_window,
            )
            wash_sale_flag = wash_sale_flag or result.wash_sale_flag
            if result.character == TaxCharacter.SHORT_TERM:
                short_term_seen = True
                realized_gain_short_term += result.realized_gain
                if (latest_short_term_opened_at is None
                        or lot.opened_at > latest_short_term_opened_at):
                    latest_short_term_opened_at = lot.opened_at
            else:   # TaxCharacter.LONG_TERM -- retirement already handled above
                long_term_seen = True
                realized_gain_long_term += result.realized_gain

        if short_term_seen and long_term_seen:
            character = "mixed"
        elif short_term_seen:
            character = TaxCharacter.SHORT_TERM.value
        else:
            character = TaxCharacter.LONG_TERM.value

        # Each component priced at ITS OWN configured rate -- a component
        # contributes nothing (not a fabricated $0) when its own rate is
        # unconfigured or it realised no gain; the whole figure is `None`
        # only when NEITHER component contributes anything.
        tax_parts = []
        if realized_gain_short_term > 0 and estimated_short_term_tax_rate is not None:
            tax_parts.append(realized_gain_short_term * estimated_short_term_tax_rate)
        if realized_gain_long_term > 0 and estimated_long_term_tax_rate is not None:
            tax_parts.append(realized_gain_long_term * estimated_long_term_tax_rate)
        estimated_tax = sum(tax_parts) if tax_parts else None

        # "When does long-term treatment begin" is only meaningful for a
        # lot not yet long-term -- the LATEST still-short-term lot is the
        # last one to cross the threshold; `None` when every disposed lot
        # is already long-term (nothing pending).
        long_term_begins = (
            (latest_short_term_opened_at
             + timedelta(days=LONG_TERM_THRESHOLD_DAYS)).isoformat()
            if latest_short_term_opened_at is not None else None
        )

        return {
            "character": character,
            "realized_gain": realized_gain_short_term + realized_gain_long_term,
            "realized_gain_short_term": realized_gain_short_term,
            "realized_gain_long_term": realized_gain_long_term,
            "estimated_tax": estimated_tax,
            "wash_sale_flag": wash_sale_flag,
            "long_term_treatment_begins": long_term_begins,
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
    approval_service: ApprovalService | None = None,
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
    # Earmarking (see module docstring): every OTHER pending BUY request's
    # own earmark for this account, BEFORE this order is sized -- this is
    # what `agent.risk.risk_constrain`'s existing reserve-scale step needed
    # and never received (agent/risk.py:72 already subtracts
    # `pending_buy_notional`; nothing upstream ever set it until now).
    other_pending_earmarks = approval_request_store.outstanding_earmarks(
        gatekeeper.account_id, now, service=approval_service)
    portfolio = PortfolioState(
        account_id=gatekeeper.account_id, nlv=nlv,
        settled_cash=float(broker_account.settled_cash),
        unsettled_cash=float(broker_account.unsettled_cash),
        pending_buy_notional=other_pending_earmarks,
    )
    opens_day_trade = any(
        f.symbol == symbol and _same_session(f.filled_at, now) for f in ledger.fills
    )

    qty = None
    broker_position_qty = None
    if side == "BUY":
        requested_notional = (max_position_pct / 100.0) * nlv
        qty = requested_notional / price_at_analysis if price_at_analysis else 0.0

        # Insufficient settled cash is a SUPPRESSION, not a gate rejection
        # (module docstring, Unit item 6): checked against the SAME shared
        # `agent.risk.investable_cash` function `risk_constrain`'s own
        # reserve-scale step calls, before `Gatekeeper.stage` is ever
        # invoked. No request is created; no gate is even reached.
        available_cash = investable_cash(portfolio, gatekeeper.risk_policy)
        if requested_notional > available_cash:
            audit_log.append(
                actor="system", action="approval_request_suppressed",
                object_type="opportunity_event", object_id=event.event_id,
                after={"reason": "insufficient_settled_cash", "symbol": symbol,
                      "side": side, "required": requested_notional,
                      "available": available_cash},
                timestamp=now,
            )
            return ApprovalTriggerResult(request=None, staged=None,
                                         suppressed_reason="insufficient_settled_cash")
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

    requests_today = approval_request_store.count_decided_on(
        market_calendar.session_for_instant(now))
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
        pending_buy_notional=other_pending_earmarks,
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
        "staged_order": _encode_staged_order(staged),
    }
    risk_result = {
        "gates_passed": list(staged.gates_passed), "binding": list(staged.binding),
        "post_trade": post_trade, "tax": tax,
    }
    band_pct = price_band_pct / 100.0
    earmark = staged.notional if side == "BUY" else 0.0
    request = approval_request_store.create(
        account_id=gatekeeper.account_id, run_id=run_id,
        proposal_snapshot=proposal_snapshot, risk_result=risk_result,
        price_at_analysis=price_at_analysis,
        price_band_low=price_at_analysis * (1 - band_pct),
        price_band_high=price_at_analysis * (1 + band_pct),
        earmark=earmark, now=now, expiration=approval_expiration,
    )
    return ApprovalTriggerResult(request=request, staged=staged, suppressed_reason=None)
