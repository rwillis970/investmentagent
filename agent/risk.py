"""Deterministic risk: dual-basis reserve and the portfolio constrainer (§6.1).

Constraints are applied to the *target weight vector* before any order exists.
Per-order enforcement is path dependent — two orders that individually pass can
jointly breach a sector cap, and the outcome then depends on arrival order.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RiskPolicy:
    version: str
    max_position_pct: float          # of NLV
    max_sector_pct: float            # of NLV
    min_settled_cash_pct_of_nlv: float
    min_absolute_settled_cash: float


@dataclass(frozen=True)
class PortfolioState:
    nlv: float
    settled_cash: float
    unsettled_cash: float = 0.0
    pending_buy_notional: float = 0.0
    estimated_fees: float = 0.0


@dataclass(frozen=True)
class ConstrainResult:
    weights: dict[str, float]
    binding: tuple[str, ...]
    investable_cash: float
    required_reserve: float

    @property
    def notional(self) -> dict[str, float]:
        return dict(self.weights)


def required_reserve(p: PortfolioState, policy: RiskPolicy) -> float:
    return max(p.nlv * policy.min_settled_cash_pct_of_nlv / 100.0,
               policy.min_absolute_settled_cash)


def investable_cash(p: PortfolioState, policy: RiskPolicy) -> float:
    """Settled cash is what can be spent; unsettled proceeds never are."""
    return max(0.0, p.settled_cash
               - p.pending_buy_notional
               - p.estimated_fees
               - required_reserve(p, policy))


def risk_constrain(target: dict[str, float], p: PortfolioState,
                   policy: RiskPolicy,
                   sectors: dict[str, str] | None = None) -> ConstrainResult:
    """Clip to per-name cap, scale offending sectors, then scale the whole book
    so the reserve is preserved. Returns the feasible weights and which
    constraints bound — the caller records both requested and authorised."""
    sectors = sectors or {}
    binding: list[str] = []
    w = {s: max(0.0, float(x)) for s, x in target.items() if x and x > 0}

    cap = policy.max_position_pct / 100.0
    for s, x in list(w.items()):
        if x > cap:
            w[s] = cap
            binding.append(f"max_position:{s}")

    sector_cap = policy.max_sector_pct / 100.0
    by_sector: dict[str, list[str]] = {}
    for s in w:
        by_sector.setdefault(sectors.get(s, "UNKNOWN"), []).append(s)
    for sector, names in by_sector.items():
        total = sum(w[s] for s in names)
        if total > sector_cap and total > 0:
            k = sector_cap / total
            for s in names:
                w[s] *= k
            binding.append(f"max_sector:{sector}")

    reserve = required_reserve(p, policy)
    cash = investable_cash(p, policy)
    gross = sum(w.values()) * p.nlv
    if gross > cash:
        k = (cash / gross) if gross > 0 else 0.0
        for s in w:
            w[s] *= k
        binding.append("settled_cash_reserve")

    w = {s: x for s, x in w.items() if x > 1e-9}

    # Post-conditions. These are the guarantees callers rely on.
    assert sum(w.values()) * p.nlv <= cash + 1e-6, "reserve breached"
    assert all(x <= cap + 1e-9 for x in w.values()), "position cap breached"

    return ConstrainResult(weights=w, binding=tuple(binding),
                           investable_cash=cash, required_reserve=reserve)
