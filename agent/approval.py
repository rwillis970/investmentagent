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
    consumed_at: datetime | None = None
    # Set only by ApprovalService.sweep_expired (§8.1 startup), never by
    # consume() -- kept distinct from consumed_at so a token retired because
    # it went stale across a restart never reads, in its own audit trail, as
    # though it had actually been spent on an order.
    swept_at: datetime | None = None

    def consume(self, *, fingerprint: str, price: float, now: datetime) -> None:
        """Atomically spend the token, or raise. Callers must treat any
        exception here as a hard stop, never as a retryable condition."""
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
                now: datetime) -> ApprovalToken:
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
        tok = ApprovalToken(
            token_id=token_id, request_id=request_id, order_fingerprint=fingerprint,
            price_band=band, expires_at=now + self.expiration, decided_at=now,
            decision_elapsed_ms=int(elapsed.total_seconds() * 1000),
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
