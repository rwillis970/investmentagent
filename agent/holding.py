"""Lot-level holding period and early exit (§4.1–4.2).

Two properties that position-level enforcement cannot provide:
  * a lot's policy is frozen at fill, so shortening the minimum hold never
    retroactively releases an open lot;
  * only settled AND eligible lots are sellable, which also implements the
    cash-account settlement constraint in §4.4.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum

from .durations import parse_duration
from .lot_selection import ALPACA_DEFAULT_POLICY, LotSelectionPolicy, disposal_order


class ExitCategory(Enum):
    STOP_LOSS = "stop_loss"
    ADVERSE_DISCLOSURE = "adverse_disclosure"
    DISTRESS_EVENT = "distress_event"          # fraud, bankruptcy, delisting, halt
    PORTFOLIO_BREACH = "portfolio_breach"
    RECONCILIATION_FAULT = "reconciliation_fault"
    MANUAL_INSTRUCTION = "manual_instruction"


EVIDENCE_EXEMPT = frozenset({ExitCategory.MANUAL_INSTRUCTION})


class HoldingViolation(Exception):
    pass


@dataclass(frozen=True)
class HoldingPolicy:
    """Versioned holding policy (§9). The registry below is the authority for
    what a version MEANS, so a lot reconstructed from storage resolves its
    duration from its own version — never from whatever config says today."""
    version: str
    minimum_holding_period: timedelta
    cooldown_period: timedelta
    effective_at: datetime | None = None


class HoldingPolicyRegistry:
    def __init__(self, policies=()):
        self._by_version: dict[str, HoldingPolicy] = {}
        for p in policies:
            self.register(p)

    def register(self, policy: HoldingPolicy) -> HoldingPolicy:
        existing = self._by_version.get(policy.version)
        if existing is not None and existing != policy:
            raise HoldingViolation(
                f"holding policy {policy.version} is already registered with "
                "different values; policy versions are immutable"
            )
        self._by_version[policy.version] = policy
        return policy

    def get(self, version: str) -> HoldingPolicy:
        try:
            return self._by_version[version]
        except KeyError as exc:
            raise HoldingViolation(
                f"unknown holding policy version {version!r}; refusing to guess "
                "a duration for an existing lot"
            ) from exc

    def make_lot(self, *, lot_id: str, account_id: str, symbol: str, qty: float,
                 cost_basis: float, opened_at: datetime, policy_version: str,
                 settles_at: datetime | None = None) -> "Lot":
        policy = self.get(policy_version)
        return Lot(lot_id=lot_id, account_id=account_id, symbol=symbol, qty=qty,
                   cost_basis=cost_basis, opened_at=opened_at,
                   minimum_hold=policy.minimum_holding_period,
                   holding_policy_version=policy.version, settles_at=settles_at)

    def lot_from_row(self, row: dict) -> "Lot":
        """Reconstruct a lot from storage. The duration comes from the version,
        and a stored duration that disagrees with the registry is an error
        rather than something to silently prefer."""
        version = row["holding_policy_version"]
        policy = self.get(version)
        stored = row.get("minimum_holding_period")
        if stored is not None:
            stored_td = (stored if isinstance(stored, timedelta)
                         else parse_duration(str(stored)))
            if stored_td != policy.minimum_holding_period:
                raise HoldingViolation(
                    f"lot {row.get('lot_id')!r} stores {stored_td} but policy "
                    f"{version} defines {policy.minimum_holding_period}"
                )
        return Lot(
            lot_id=row["lot_id"], account_id=row["account_id"],
            symbol=row["symbol"], qty=row["qty"],
            cost_basis=row["cost_basis"], opened_at=row["opened_at"],
            minimum_hold=policy.minimum_holding_period,
            holding_policy_version=version, settles_at=row.get("settles_at"),
            closed_at=row.get("closed_at"),
        )


@dataclass(frozen=True)
class Lot:
    lot_id: str
    account_id: str                # required: a lot belongs to one account
    symbol: str
    qty: float
    cost_basis: float
    opened_at: datetime           # the FILL timestamp, never order submit
    minimum_hold: timedelta       # frozen at fill
    holding_policy_version: str
    settles_at: datetime | None = None
    closed_at: datetime | None = None

    @property
    def earliest_normal_exit_at(self) -> datetime:
        return self.opened_at + self.minimum_hold

    def is_settled(self, now: datetime) -> bool:
        return self.settles_at is None or now >= self.settles_at

    def is_hold_eligible(self, now: datetime) -> bool:
        return now >= self.earliest_normal_exit_at

    def is_open(self) -> bool:
        return self.closed_at is None

    def remaining_hold(self, now: datetime) -> timedelta:
        return max(timedelta(0), self.earliest_normal_exit_at - now)


@dataclass(frozen=True)
class EarlyExitRequest:
    request_id: str
    lot_id: str
    category: ExitCategory
    evidence_fact_ref: str | None
    remaining_hold: timedelta
    requested_at: datetime


def open_lots(lots, account_id: str, symbol: str | None = None):
    """account_id is required, not optional -- filtering by symbol alone
    would net two accounts' shares of the same symbol together, which is
    exactly the cross-account netting bug the multi-account addendum exists
    to prevent."""
    return [l for l in lots if l.is_open() and l.account_id == account_id
            and (symbol is None or l.symbol == symbol)]


def sellable_qty(lots, account_id: str, symbol: str, now: datetime, *,
                 lot_selection_policy: LotSelectionPolicy = ALPACA_DEFAULT_POLICY
                 ) -> float:
    """Quantity a normal strategy exit may sell right now, for THIS account.

    REVIEW FIX (Commit 5): an internal `lot_id` does not control which lot
    the broker actually disposes of -- Alpaca's confirmed default matches
    sells in FIFO order across ALL open lots for a symbol, not whichever
    lot our own bookkeeping happens to think is being sold (see
    `agent.lot_selection` for the citations). This used to sum the qty of
    every INDIVIDUALLY hold-eligible-and-settled lot, ignoring order
    entirely -- which is wrong whenever an older, FIFO-first lot is still
    inside its hold while a newer lot happens to already be eligible (e.g.
    opened under a shorter policy version). In that case the broker would
    consume the older, blocked lot FIRST on any sell, regardless of which
    lot we believed we were selling -- so that older lot's ineligibility
    must block everything behind it in FIFO order, not just its own qty.

    The fix: walk lots in the broker's actual disposal order and sum only
    the MAXIMAL LEADING RUN of lots that are each settled and past their
    own minimum hold. The first lot that fails either check stops the sum
    there -- everything from that lot onward is unreachable until it is
    disposed of, no matter how eligible a later lot looks in isolation.

    NO OVERRIDE OR BYPASS PATH: `disposal_order` raises
    `UnsupportedLotSelectionPolicy` for any method other than the one
    confirmed broker default, and that exception is left to propagate
    here, uncaught -- a gate that falls back to a guessed ordering when it
    can't determine the real one is not a gate."""
    ordered = disposal_order(lot_selection_policy, open_lots(lots, account_id, symbol))
    total = 0.0
    for l in ordered:
        if not (l.is_hold_eligible(now) and l.is_settled(now)):
            break
        total += l.qty
    return total


def blocked_qty(lots, account_id: str, symbol: str, now: datetime, *,
                lot_selection_policy: LotSelectionPolicy = ALPACA_DEFAULT_POLICY
                ) -> float:
    """Everything open for this symbol that `sellable_qty` does not count --
    including lots that would look individually eligible but sit behind an
    ineligible FIFO predecessor (see `sellable_qty`'s docstring)."""
    ordered = open_lots(lots, account_id, symbol)
    total_open = sum(l.qty for l in ordered)
    return total_open - sellable_qty(lots, account_id, symbol, now,
                                     lot_selection_policy=lot_selection_policy)


def check_normal_exit(lot: Lot, now: datetime) -> None:
    """Raise unless a normal (non-exception) exit is permitted."""
    if not lot.is_open():
        raise HoldingViolation(f"lot {lot.lot_id} is already closed")
    if not lot.is_settled(now):
        raise HoldingViolation(
            f"lot {lot.lot_id} is unsettled until {lot.settles_at.isoformat()}; "
            "selling it would risk a good-faith violation (§4.4)"
        )
    if not lot.is_hold_eligible(now):
        raise HoldingViolation(
            f"lot {lot.lot_id} is held until {lot.earliest_normal_exit_at.isoformat()} "
            f"({lot.remaining_hold(now)} remaining) under policy "
            f"{lot.holding_policy_version}; an early exit requires an evidenced "
            "exception and approval (§4.2)"
        )


def request_early_exit(lot: Lot, *, request_id: str, category: ExitCategory,
                       evidence_fact_ref: str | None, now: datetime
                       ) -> EarlyExitRequest:
    """Build an early-exit request, or refuse. Never a silent bypass: even a
    valid request only produces an approval card, never an order."""
    if not isinstance(category, ExitCategory):
        raise HoldingViolation(f"unknown early-exit category {category!r}")
    if category not in EVIDENCE_EXEMPT and not evidence_fact_ref:
        raise HoldingViolation(
            f"category {category.value} requires a fact reference as evidence (§4.2)"
        )
    if lot.is_hold_eligible(now) and lot.is_settled(now):
        raise HoldingViolation(
            "lot is already eligible for a normal exit; no exception needed"
        )
    return EarlyExitRequest(
        request_id=request_id, lot_id=lot.lot_id, category=category,
        evidence_fact_ref=evidence_fact_ref,
        remaining_hold=lot.remaining_hold(now), requested_at=now,
    )
