"""Gate composition — the Figure 1 pipeline (§3, §5.1).

The gates exist as independent, individually tested objects. This module is
what puts them in the one order that money actually flows through, so that
"the capability gate is enforced" is a property of the system rather than of a
unit test. Nothing else in the codebase should assemble these checks itself.

    capability (universe) -> holding eligibility -> day-trade guard
      -> risk_constrain (capability gate 2, position cap, sector cap, reserve)
      -> capability (pre-submit) -> signed StagedOrder

A StagedOrder is not an order. It still requires an approval token in live
mode, and `BrokerAdapter.submit` re-checks capability at gate 4 before
submission — independently, not by trusting this module's signature.

WHY risk_constrain AND NOT AN INLINE CHECK. §6.1/§1's invariant is that risk
is applied to the *target weight vector*, never per order after the fact.
`stage()` therefore builds the post-trade target weight vector (current
holdings plus this order) and runs the one shared `risk_constrain` over it,
the same function the strategy layer uses to size a whole rebalance. If the
authorised weight for this symbol comes back lower than what was requested,
the order is RESIZED to the authorised amount rather than rejected outright;
if the authorised weight is zero, it is rejected. Position and sector caps
therefore apply on this path.

CLOSING THE PIPELINE-BYPASS GAP. There used to be a keyword-argument form of
`BrokerAdapter.submit` (symbol=..., side=..., qty=..., ...) that let any
caller skip straight to the broker, bypassing this module's gates entirely --
reserve, holding eligibility, the day-trade guard, and position/sector caps.
`submit` now takes only a signed `StagedOrder` produced by `Gatekeeper.stage`,
and verifies the signature before doing anything else (see broker/base.py).

ORDER KINDS (§8.3). `side` is one of BUY, SELL, CANCEL, CLOSE, REPLACE:

  - BUY / SELL: the original path. Full capability, holding (SELL), day-trade
    and risk gates.
  - CANCEL: capability and signature only, deliberately. A risk limit or a
    stale holding fact must never be able to trap an order still resting in
    the market, so risk_constrain, the holding gate and the day-trade guard
    are all skipped outright for a cancel — not evaluated-and-ignored, never
    reached at all.
  - CLOSE: the full risk_constrain path, same shape as a SELL, but the
    quantity is resolved from `broker_position_qty` — the caller's reconciled
    read of the broker's own position — never from a caller-supplied belief
    about how much is held. The holding gate still applies: a close cannot
    evade the minimum-hold policy that a plain SELL would be subject to.
  - REPLACE: the enum member and StagedOrder shape exist; `stage()` raises
    NotImplementedError immediately. One order in flight at a time means
    cancel-then-submit is sufficient for the pilot — a deliberate scope cut,
    not an oversight.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass, field
from datetime import datetime

from . import market_calendar
from .accounts import AccountType, CrossAccountError
from .daytrade import DayTradeBlocked, DayTradeGuard
from .holding import sellable_qty
from .policy import Gate, PolicyViolation, TradeCapabilityPolicy
from .risk import PortfolioState, RiskPolicy, risk_constrain

# The order kinds `Gatekeeper.stage` accepts as `side`. REPLACE is named here
# so its rejection is a deliberate, documented branch rather than a fallthrough
# to "unknown side".
ORDER_SIDES = ("BUY", "SELL", "CANCEL", "CLOSE", "REPLACE")

# Fields that make up a StagedOrder's identity for signing purposes.
# Deliberately excludes `signature` itself. `client_order_id` doubles as the
# binding target for a CANCEL: for BUY/SELL/CLOSE it is the id of the new
# order being created; for CANCEL it is the id of the existing order being
# cancelled. Either way it is signed, so a StagedOrder minted to cancel order
# A cannot be altered to cancel order B without invalidating the signature.
#
# `lot_id` (Commit b, fills-reach-the-ledger unit): the lot our strategy
# intends to reduce is part of what a human approves for a SELL -- holding
# period, wash-sale classification and cost basis all depend on which lot
# this is. Covered here so it cannot be swapped post-staging without
# invalidating the signature the same way `symbol`/`side`/`authorized_qty`
# already can't be -- see test_gate_integrity.py's tampering test, extended
# to cover this field too.
_SIGNABLE_FIELDS = (
    "account_id", "client_order_id", "symbol", "side", "requested_qty",
    "authorized_qty", "order_type", "time_in_force", "limit_price",
    "asset_class", "funding", "session", "requested_notional", "notional",
    "gates_passed", "binding", "lot_id",
)


def _canonical(values: dict) -> str:
    return json.dumps(values, sort_keys=True, separators=(",", ":"), default=str)


def sign_staged_order(values: dict, key: bytes) -> str:
    """HMAC-SHA256 over a StagedOrder's material fields, keyed by a per-process
    secret the Gatekeeper holds.

    THIS IS NOT A SECURITY BOUNDARY. Anyone who can import this module can
    call this function with any key. It does not defend against a malicious
    author of this codebase, and it is not a substitute for authentication,
    encryption, or a real HSM-backed signing scheme. What it defends against
    is ACCIDENT: constructing a StagedOrder by hand, or calling
    `adapter.submit(...)` with ad hoc fields instead of routing through
    `Gatekeeper.stage`, now fails loudly with `StagingForged` instead of
    silently filling an order that skipped reserve, holding, day-trade,
    position and sector caps. That is the entire threat model.
    """
    return hmac.new(key, _canonical(values).encode(), hashlib.sha256).hexdigest()


class Rejected(Exception):
    """Raised by the gatekeeper. Every rejection names the gate that produced
    it, so the audit record says where the decision died."""

    def __init__(self, gate: str, reason: str):
        self.gate, self.reason = gate, reason
        super().__init__(f"[{gate}] {reason}")


@dataclass(frozen=True)
class StagedOrder:
    account_id: str                     # required: which account this order belongs to
    client_order_id: str
    symbol: str
    side: str
    requested_qty: float
    authorized_qty: float
    order_type: str
    time_in_force: str
    limit_price: float | None
    asset_class: str
    funding: str
    session: str
    requested_notional: float
    notional: float                     # authorized_qty * price — what may actually submit
    gates_passed: tuple[str, ...]
    binding: tuple[str, ...]            # every constraint that resized or would have rejected
    signature: str
    # The lot our strategy intends to reduce. Meaningful for SELL only --
    # None for BUY (a buy creates a new lot, it doesn't reduce one), and
    # None for CANCEL/CLOSE (a cancel touches no lot; a close's qty spans
    # whatever's open across possibly-multiple lots, which this one-lot_id
    # field cannot represent -- see Gatekeeper.stage's own validation and
    # this unit's delivery report for that named, unsolved gap). Covered by
    # the signature -- see _SIGNABLE_FIELDS above.
    lot_id: str | None = None

    @property
    def qty(self) -> float:
        """The only quantity `BrokerAdapter.submit` will ever see."""
        return self.authorized_qty

    def _signable(self) -> dict:
        return {name: getattr(self, name) for name in _SIGNABLE_FIELDS}

    def verify(self, key: bytes) -> bool:
        if not self.signature:
            return False
        expected = sign_staged_order(self._signable(), key)
        return hmac.compare_digest(expected, self.signature)


@dataclass
class Gatekeeper:
    """One Gatekeeper per account_id. Its signing_key is per-instance, so a
    StagedOrder it produces cannot verify against a different account's
    adapter even with a matching account_id -- the two gaps are independent
    layers, not one check doing double duty."""
    account_id: str
    account_type: AccountType
    capability_policy: TradeCapabilityPolicy
    risk_policy: RiskPolicy
    day_trade_guard: DayTradeGuard
    live: bool = False
    # One random key per Gatekeeper instance (per process, in the pilot's
    # single-process deployment). The matching adapter is wired to the same
    # key via `attach_staging_key` — see broker/base.py.
    signing_key: bytes = field(default_factory=lambda: secrets.token_bytes(32),
                               repr=False)

    def __post_init__(self) -> None:
        if self.day_trade_guard.account_id != self.account_id:
            raise CrossAccountError(self.account_id, self.day_trade_guard.account_id,
                                    "Gatekeeper.__init__ day_trade_guard")

    def stage(self, *, client_order_id: str, symbol: str, side: str,
              order_type: str, time_in_force: str,
              portfolio: PortfolioState, now: datetime,
              posture: str,
              qty: float | None = None, price: float | None = None,
              limit_price: float | None = None,
              asset_class: str = "US_EQUITY", funding: str = "SETTLED_CASH",
              session: str = "REGULAR", lots=(),
              opens_day_trade: bool = False,
              sectors: dict[str, str] | None = None,
              current_weights: dict[str, float] | None = None,
              broker_position_qty: float | None = None,
              lot_id: str | None = None) -> StagedOrder:
        side_u = side.upper()

        if side_u == "REPLACE":
            raise NotImplementedError(
                "REPLACE is part of the StagedOrder vocabulary (order kinds: "
                f"{ORDER_SIDES}) but is deliberately not implemented for this "
                "pilot: one order in flight at a time means cancel-then-submit "
                "is sufficient. Do not add a REPLACE code path without "
                "revisiting that decision first."
            )

        if portfolio.account_id != self.account_id:
            raise CrossAccountError(self.account_id, portfolio.account_id,
                                    "Gatekeeper.stage portfolio")

        passed: list[str] = []
        binding: tuple[str, ...] = ()
        dims = dict(asset_class=asset_class, side=side_u, funding=funding,
                    order_type=order_type, session=session,
                    time_in_force=time_in_force)
        sectors = sectors or {}
        current_weights = current_weights or {}

        # 1. capability, at universe construction. Runs for every order kind
        #    without exception -- a cancel on a disabled asset class is still
        #    refused. Capability is never optional; only risk is skipped, and
        #    only for CANCEL specifically.
        try:
            self.capability_policy.check(gate=Gate.UNIVERSE, live=self.live,
                                         symbol=symbol, **dims)
        except PolicyViolation as exc:
            raise Rejected("capability:universe", str(exc)) from exc
        passed.append("capability:universe")

        # lot_id (Commit b): meaningful for a SELL only, and required there --
        # the lot our strategy intends to reduce is part of what gets
        # approved (holding period, wash-sale classification and cost basis
        # all depend on which lot this is), so it is validated here, before
        # any gate below, and carried into the signed fields. A BUY creates
        # a new lot rather than reducing one; CANCEL touches no lot; CLOSE's
        # quantity can span multiple open lots, which this single lot_id
        # field cannot represent (a known, unsolved gap -- see this unit's
        # delivery report) -- so all three require lot_id to be None, not
        # silently ignored if supplied.
        if side_u == "SELL":
            if lot_id is None:
                raise Rejected(
                    "holding",
                    "a SELL must specify the intended lot_id (the lot the "
                    "strategy intends to reduce); it is part of what is "
                    "approved and is covered by the StagedOrder signature",
                )
            if not any(l.lot_id == lot_id and l.account_id == self.account_id
                      and l.symbol == symbol and l.is_open() for l in lots):
                raise Rejected(
                    "holding",
                    f"lot_id {lot_id!r} is not an open lot for {symbol} in "
                    f"account {self.account_id!r}; refusing to sign a SELL "
                    "against a lot that does not exist",
                )
        elif lot_id is not None:
            raise Rejected(
                "holding",
                f"lot_id is only meaningful for a SELL; got side={side_u!r} "
                f"with lot_id={lot_id!r}",
            )

        if side_u == "CANCEL":
            return self._stage_cancel(
                client_order_id=client_order_id, symbol=symbol, side_u=side_u,
                order_type=order_type, time_in_force=time_in_force,
                limit_price=limit_price, asset_class=asset_class,
                funding=funding, session=session, dims=dims, passed=passed,
            )

        # CLOSE's defining property: the quantity is resolved from the
        # broker's own reconciled position, never from a caller-supplied
        # belief. Checked up front, before holding or risk, because it is a
        # precondition of the whole action rather than either gate's concern.
        if side_u == "CLOSE" and broker_position_qty is None:
            raise Rejected(
                "risk",
                "CLOSE requires broker_position_qty from reconciled broker "
                "state; a caller-supplied qty is not trusted for a close",
            )

        # 2. holding eligibility. SELL and CLOSE both reduce a position, so
        #    both answer to the frozen-at-fill holding policy -- a CLOSE is
        #    not a way to evade the minimum hold that a plain SELL would face.
        if side_u in ("SELL", "CLOSE"):
            check_qty = qty if side_u == "SELL" else broker_position_qty
            if check_qty is None:
                raise Rejected("holding", "no quantity to check against the holding policy")
            available = sellable_qty(lots, self.account_id, symbol, now)
            if check_qty > available + 1e-9:
                raise Rejected(
                    "holding",
                    f"requested {check_qty} but only {available} is settled and past "
                    "its minimum hold; an early exit needs an evidenced "
                    "exception and approval (§4.2)",
                )
            passed.append("holding")

        # 3. day-trade guard. as_of is derived from `now` (the moment this
        #    order is being staged), not supplied by the caller -- the
        #    market calendar unit (§11 Day 4) removed the caller-supplied
        #    session-list form of this check because a wrong window
        #    miscounted silently; there is nothing left here for a caller to
        #    get wrong.
        if opens_day_trade:
            as_of = market_calendar.session_for_instant(now)
            try:
                self.day_trade_guard.check(as_of, posture=posture)
            except DayTradeBlocked as exc:
                raise Rejected("day_trade", str(exc)) from exc
            passed.append("day_trade")

        # 4. risk — buys and closes only. Build the post-trade target weight
        #    vector and run it through the one shared risk_constrain
        #    (capability gate 2, position cap, sector cap, reserve), then diff
        #    against what was requested. Resize if lower; reject if zero.
        #    Never per-order after the fact — this is the whole vector, same
        #    as the strategy layer would produce for a rebalance.
        requested_qty = qty
        authorized_qty = qty
        requested_notional = (qty or 0.0) * (price or 0.0)
        notional = requested_notional

        if side_u == "BUY":
            requested_weight = requested_notional / portfolio.nlv if portfolio.nlv else 0.0
            current_weight = current_weights.get(symbol, 0.0)
            target = dict(current_weights)
            target[symbol] = current_weight + requested_weight

            result = risk_constrain(
                target, portfolio, self.risk_policy, sectors,
                capability_policy=self.capability_policy,
                asset_classes={symbol: asset_class}, live=self.live,
            )
            authorized_weight = result.weights.get(symbol, 0.0)
            delta_weight = max(0.0, authorized_weight - current_weight)
            authorized_qty = (delta_weight * portfolio.nlv) / price if price else 0.0
            notional = authorized_qty * price
            binding = result.binding

            if authorized_qty <= 1e-9:
                reason = (
                    f"policy authorizes 0 of the requested {requested_qty} "
                    f"({requested_notional:.2f}); binding: "
                    f"{', '.join(binding) or 'capability'} (§6.1, §5.1 gate 2)"
                )
                raise Rejected("risk", reason)
            passed.append("risk")

        elif side_u == "CLOSE":
            if price is None:
                raise Rejected("risk", "CLOSE requires a price to value the reconciled position")
            requested_qty = broker_position_qty
            requested_notional = broker_position_qty * price
            notional = requested_notional
            current_weight = (broker_position_qty * price) / portfolio.nlv if portfolio.nlv else 0.0
            target = dict(current_weights)
            target[symbol] = 0.0

            result = risk_constrain(
                target, portfolio, self.risk_policy, sectors,
                capability_policy=self.capability_policy,
                asset_classes={symbol: asset_class}, live=self.live,
            )
            authorized_weight = result.weights.get(symbol, 0.0)
            delta_weight = max(0.0, current_weight - authorized_weight)
            authorized_qty = (delta_weight * portfolio.nlv) / price if price else 0.0
            notional = authorized_qty * price
            binding = result.binding

            if authorized_qty <= 1e-9:
                reason = (
                    f"policy authorizes closing 0 of the reconciled position "
                    f"({requested_qty}); binding: {', '.join(binding) or 'capability'} "
                    "(§6.1, §5.1 gate 2)"
                )
                raise Rejected("risk", reason)
            passed.append("risk")

        # 5. capability, immediately before submission
        try:
            self.capability_policy.check(gate=Gate.PRE_SUBMIT, live=self.live,
                                         symbol=symbol, **dims)
        except PolicyViolation as exc:
            raise Rejected("capability:pre_submit", str(exc)) from exc
        passed.append("capability:pre_submit")

        fields = dict(
            account_id=self.account_id,
            client_order_id=client_order_id, symbol=symbol, side=side_u,
            requested_qty=requested_qty, authorized_qty=authorized_qty,
            order_type=order_type, time_in_force=time_in_force,
            limit_price=limit_price, asset_class=asset_class, funding=funding,
            session=session, requested_notional=requested_notional,
            notional=notional, gates_passed=tuple(passed), binding=binding,
            lot_id=lot_id,
        )
        signature = sign_staged_order(fields, self.signing_key)
        return StagedOrder(**fields, signature=signature)

    def _stage_cancel(self, *, client_order_id: str, symbol: str, side_u: str,
                       order_type: str, time_in_force: str,
                       limit_price: float | None, asset_class: str,
                       funding: str, session: str, dims: dict,
                       passed: list[str]) -> StagedOrder:
        """CANCEL skips holding, day-trade and risk_constrain entirely (§8.3).
        A risk limit or a stale holding fact must never be able to trap an
        order that's still resting in the market -- so those gates are never
        reached for a cancel, not evaluated and overridden. Capability and the
        HMAC signature are the only checks a cancel gets, and that is by
        design.
        """
        try:
            self.capability_policy.check(gate=Gate.PRE_SUBMIT, live=self.live,
                                         symbol=symbol, **dims)
        except PolicyViolation as exc:
            raise Rejected("capability:pre_submit", str(exc)) from exc
        passed.append("capability:pre_submit")

        fields = dict(
            account_id=self.account_id,
            client_order_id=client_order_id, symbol=symbol, side=side_u,
            requested_qty=0.0, authorized_qty=0.0,
            order_type=order_type, time_in_force=time_in_force,
            limit_price=limit_price, asset_class=asset_class, funding=funding,
            session=session, requested_notional=0.0, notional=0.0,
            gates_passed=tuple(passed), binding=(), lot_id=None,
        )
        signature = sign_staged_order(fields, self.signing_key)
        return StagedOrder(**fields, signature=signature)
