"""Lot-level holding period and early exit (§4.1–4.2).

Two properties that position-level enforcement cannot provide:
  * a lot's policy is frozen at fill, so shortening the minimum hold never
    retroactively releases an open lot;
  * only settled AND eligible lots are sellable, which also implements the
    cash-account settlement constraint in §4.4.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum


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
class Lot:
    lot_id: str
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


def open_lots(lots, symbol: str | None = None):
    return [l for l in lots if l.is_open() and (symbol is None or l.symbol == symbol)]


def sellable_qty(lots, symbol: str, now: datetime) -> float:
    """Quantity a normal strategy exit may sell right now."""
    return sum(l.qty for l in open_lots(lots, symbol)
               if l.is_hold_eligible(now) and l.is_settled(now))


def blocked_qty(lots, symbol: str, now: datetime) -> float:
    return sum(l.qty for l in open_lots(lots, symbol)
               if not (l.is_hold_eligible(now) and l.is_settled(now)))


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
