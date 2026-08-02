"""Single-use approval token (§9, §12 criterion 13).

Human approval is the central control of the pilot, so it is a *token* rather
than a boolean: bound to an order fingerprint and a price band, expiring, and
consumed atomically at submission. A replay, a duplicate run or a restart
cannot reuse it, and an order whose parameters drift from what was approved
cannot find a valid token.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta


class ApprovalError(Exception):
    pass


class TokenExpired(ApprovalError):
    pass


class TokenConsumed(ApprovalError):
    pass


class OrderMismatch(ApprovalError):
    pass


class PriceOutOfBand(ApprovalError):
    pass


class TokenReissued(ApprovalError):
    pass


def order_fingerprint(*, symbol: str, side: str, qty: float, order_type: str,
                      time_in_force: str, limit_price: float | None = None,
                      lot_id: str | None = None) -> str:
    """A human's approval token is bound to this hash. `lot_id` (Commit 3,
    2026-07-30) closes a gap where a SELL's approval covered symbol/side/
    qty/type/TIF/limit but not which lot it reduces -- even though lot
    choice determines holding-period compliance and cost basis, and
    `agent.pipeline._SIGNABLE_FIELDS` already treats lot_id as
    signature-critical in the separate staging HMAC for the same reason.
    Defaults to None so every existing BUY/CANCEL call site (which never
    had a lot to bind) hashes identically to before this parameter
    existed."""
    body = json.dumps({
        "symbol": symbol.upper(), "side": side.upper(), "qty": round(float(qty), 6),
        "order_type": order_type.upper(), "time_in_force": time_in_force.upper(),
        "limit_price": None if limit_price is None else round(float(limit_price), 4),
        "lot_id": lot_id,
    }, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode()).hexdigest()[:32]


@dataclass
class ApprovalToken:
    token_id: str
    request_id: str
    order_fingerprint: str
    price_band: tuple[float, float]
    expires_at: datetime
    decided_at: datetime
    decision_elapsed_ms: int
    # ORIGINAL ORDER, IN THE CLEAR (unattended wiring unit, 2026-08-01,
    # §10's modify-within-bounds requirement). `order_fingerprint` above is
    # a one-way hash -- useful for exact-match detection, useless for
    # answering "is this NEW, modified order a conservative subset of what
    # was approved" (you cannot recover the original qty/limit_price from a
    # hash to compare against). These fields carry the approved order's own
    # real values so `verify_modification_within_bounds` (below) can do
    # that comparison. Set once at mint time (`ApprovalService.approve`),
    # never mutated.
    original_symbol: str = ""
    original_side: str = ""
    original_qty: float = 0.0
    original_order_type: str = ""
    original_time_in_force: str = ""
    original_limit_price: float | None = None
    original_lot_id: str | None = None
    # SERVER-RECORDED SURFACED-AT INSTANT AND ITS GOVERNING MINIMUM DISPLAY
    # TIME (§10, unattended wiring unit). Both frozen onto the token at
    # mint time from data `ApprovalService.approve` itself receives -- never
    # supplied by whoever calls `consume()`/`submit()` later. This is what
    # lets the check be re-verified AT THE POINT THE TOKEN IS CONSUMED
    # (agent.broker.base.BrokerAdapter.submit, `verify_minimum_display_time`
    # below) using only data the server itself recorded, closing the gap
    # where a UI could simply not call the check, or could lie about
    # `shown_at`, and nothing downstream would ever re-verify it.
    shown_at: datetime | None = None
    min_display: timedelta | None = None
    consumed_at: datetime | None = None
    # Set only by ApprovalService.sweep_expired (§8.1 startup), never by
    # consume() -- kept distinct from consumed_at so a token retired because
    # it went stale across a restart never reads, in its own audit trail, as
    # though it had actually been spent on an order.
    swept_at: datetime | None = None

    def consume(self, *, fingerprint: str, price: float, now: datetime) -> None:
        """Atomically spend the token, or raise. Callers must treat any
        exception here as a hard stop, never as a retryable condition.

        UNCHANGED SIGNATURE AND BEHAVIOUR (unattended wiring unit,
        2026-08-01): still an exact-fingerprint check, exactly as before.
        Modify-within-bounds is NOT implemented by loosening this check --
        see `verify_modification_within_bounds` below, which
        `BrokerAdapter.submit` calls FIRST, before ever calling this method,
        precisely so this method's own contract ("the fingerprint that is
        consumed is the fingerprint that was approved") never has to bend."""
        if self.consumed_at is not None:
            raise TokenConsumed(
                f"token {self.token_id} was already consumed at "
                f"{self.consumed_at.isoformat()}"
            )
        if self.swept_at is not None or now >= self.expires_at:
            raise TokenExpired(
                f"token {self.token_id} expired at {self.expires_at.isoformat()}"
                + ("" if self.swept_at is None else
                   f" and was swept as stale at startup ({self.swept_at.isoformat()})")
            )
        if fingerprint != self.order_fingerprint:
            raise OrderMismatch(
                "order does not match what was approved; a fresh decision is required"
            )
        low, high = self.price_band
        if not low <= price <= high:
            raise PriceOutOfBand(
                f"price {price} outside approved band [{low}, {high}]; "
                "the approval is invalidated (§3.3)"
            )
        self.consumed_at = now


def verify_minimum_display_time(token: ApprovalToken, *, now: datetime) -> None:
    """§10, enforced WHERE THE TOKEN IS CONSUMED (`BrokerAdapter.submit`),
    not by trusting the UI that called `ApprovalService.approve()`.
    `ApprovalService.approve()` already checks this once, at decision time,
    against a caller-supplied `shown_at` -- a UI that never called it, or
    that lied about `shown_at`, would defeat that check entirely. This
    re-checks the SAME property from data the server itself recorded onto
    the token at mint time (`token.shown_at`/`token.min_display`), at the
    one place a UI cannot be the enforcement point: order submission
    itself. Raises `ApprovalError` if insufficient real wall-clock time has
    elapsed; a no-op if the token predates this check (`shown_at`/
    `min_display` both `None` -- see `ApprovalToken`'s own field defaults;
    every token minted through `ApprovalService.approve` after this unit
    always has both set)."""
    if token.shown_at is None or token.min_display is None:
        return
    elapsed = now - token.shown_at
    if elapsed < token.min_display:
        raise ApprovalError(
            f"card shown for {elapsed.total_seconds():.1f}s; minimum is "
            f"{token.min_display.total_seconds():.0f}s before the token may "
            "be consumed (§10) -- re-checked at submission against the "
            "server-recorded surfaced-at instant, independent of whatever "
            "was reported at approve() time"
        )


def verify_modification_within_bounds(token: ApprovalToken, *, symbol: str, side: str,
                                      qty: float, order_type: str, time_in_force: str,
                                      limit_price: float | None,
                                      lot_id: str | None = None) -> None:
    """§10's modify-within-bounds rule, enforced WHERE THE TOKEN IS
    CONSUMED, not at whatever UI/edge assembled the modified order.
    Quantity or notional may be REDUCED and a limit price may move
    ADVERSELY TO THE TRADE (lower for a BUY -- willing to pay less; higher
    for a SELL/CLOSE -- willing to accept more) without invalidating the
    approval. Anything else -- a larger quantity, a limit moved FAVOURABLY
    (which loosens the constraint the approval was granted against), or a
    changed symbol/side/order_type/time_in_force/lot_id -- is a different
    order, not a modification, and invalidates: raises `OrderMismatch`, the
    same exception an exact-fingerprint mismatch already raises, so a
    caller cannot tell "genuinely different order" from "out-of-bounds
    modification" and must treat both as requiring a fresh decision.

    `BrokerAdapter.submit` calls this BEFORE `ApprovalToken.consume`, using
    the token's own `original_*` fields (set once at mint time, in the
    clear -- see `ApprovalToken`'s own docstring for why a fingerprint hash
    alone cannot support this comparison) -- never a value the submission
    caller supplies about what was originally approved."""
    if (symbol.upper() != token.original_symbol.upper()
            or side.upper() != token.original_side.upper()
            or order_type.upper() != token.original_order_type.upper()
            or time_in_force.upper() != token.original_time_in_force.upper()
            or lot_id != token.original_lot_id):
        raise OrderMismatch(
            "order's symbol/side/order_type/time_in_force/lot_id differs from "
            "what was approved; a fresh decision is required"
        )
    if qty > token.original_qty + 1e-9:
        raise OrderMismatch(
            f"modified qty {qty} exceeds the approved qty {token.original_qty}; "
            "size may only be reduced without re-analysis (§10)"
        )
    if limit_price is None or token.original_limit_price is None:
        if limit_price != token.original_limit_price:
            raise OrderMismatch(
                "limit_price presence changed from what was approved; a "
                "fresh decision is required"
            )
        return
    if side.upper() == "BUY":
        if limit_price > token.original_limit_price + 1e-9:
            raise OrderMismatch(
                f"modified limit_price {limit_price} is above the approved "
                f"{token.original_limit_price}; a BUY limit may only move "
                "down (adversely to the trade) without re-analysis (§10)"
            )
    else:
        if limit_price < token.original_limit_price - 1e-9:
            raise OrderMismatch(
                f"modified limit_price {limit_price} is below the approved "
                f"{token.original_limit_price}; a SELL/CLOSE limit may only "
                "move up (adversely to the trade) without re-analysis (§10)"
            )


@dataclass
class ApprovalService:
    expiration: timedelta
    min_display: timedelta
    max_per_day: int
    price_band_pct: float = 1.0
    _issued_today: dict = field(default_factory=dict)
    _tokens: dict[str, ApprovalToken] = field(default_factory=dict)

    def can_request(self, day, *, is_stop_loss: bool = False) -> bool:
        # A stop-loss exception may bypass the cap; nothing else may (§4.3).
        return is_stop_loss or self._issued_today.get(day, 0) < self.max_per_day

    def note_request(self, day) -> None:
        self._issued_today[day] = self._issued_today.get(day, 0) + 1

    def approve(self, *, token_id: str, request_id: str, fingerprint: str,
                price_at_analysis: float, shown_at: datetime,
                now: datetime, symbol: str = "", side: str = "", qty: float = 0.0,
                order_type: str = "", time_in_force: str = "",
                limit_price: float | None = None,
                lot_id: str | None = None) -> ApprovalToken:
        # A token_id is issued once. Re-approving would mint a fresh,
        # unconsumed token and silently reset the single-use guarantee — which
        # is exactly what a replayed inbox event or a restart would do.
        if token_id in self._tokens:
            raise TokenReissued(
                f"token {token_id} has already been issued"
                + (" and consumed" if self._tokens[token_id].consumed_at else "")
                + "; a new decision requires a new token id"
            )
        elapsed = now - shown_at
        if elapsed < self.min_display:
            raise ApprovalError(
                f"card shown for {elapsed.total_seconds():.1f}s; minimum is "
                f"{self.min_display.total_seconds():.0f}s before approve is enabled (§10)"
            )
        band = (price_at_analysis * (1 - self.price_band_pct / 100.0),
                price_at_analysis * (1 + self.price_band_pct / 100.0))
        # `symbol`/`side`/`qty`/`order_type`/`time_in_force`/`limit_price`/
        # `lot_id` default to empty/zero/None so every pre-existing caller
        # that only ever exercised the exact-fingerprint path keeps working
        # unchanged (unattended wiring unit, 2026-08-01) -- but a caller that
        # wants `verify_modification_within_bounds` to ever succeed for this
        # token MUST pass the real approved order here; the fingerprint
        # alone cannot reconstruct them (see ApprovalToken's own docstring).
        tok = ApprovalToken(
            token_id=token_id, request_id=request_id, order_fingerprint=fingerprint,
            price_band=band, expires_at=now + self.expiration, decided_at=now,
            decision_elapsed_ms=int(elapsed.total_seconds() * 1000),
            original_symbol=symbol, original_side=side, original_qty=qty,
            original_order_type=order_type, original_time_in_force=time_in_force,
            original_limit_price=limit_price, original_lot_id=lot_id,
            shown_at=shown_at, min_display=self.min_display,
        )
        self._tokens[token_id] = tok
        return tok

    def sweep_expired(self, *, now: datetime) -> list[ApprovalToken]:
        """§8.1 startup: `consume()` only ever notices a token is stale when
        something tries to spend it, so a token that was live at shutdown is
        still live in memory at startup until an order attempts to use it --
        which, for a token approved against a specific fingerprint and price
        band, may never happen again. This walks every issued token once and
        explicitly retires any that are already past `expires_at` and not
        yet consumed or already swept, returning them (issuance order) so
        the caller can audit each one -- this is a state change, not a
        query, and §8.1 requires it be audited."""
        swept = []
        for tok in self._tokens.values():
            if tok.consumed_at is None and tok.swept_at is None and now >= tok.expires_at:
                tok.swept_at = now
                swept.append(tok)
        return swept

    def rubber_stamp_risk(self, decisions: list[ApprovalToken]) -> bool:
        """§3.4 — surfaced on the dashboard, not enforced silently."""
        if len(decisions) < 5:
            return False
        times = sorted(d.decision_elapsed_ms for d in decisions)
        median = times[len(times) // 2]
        return median < 20_000
