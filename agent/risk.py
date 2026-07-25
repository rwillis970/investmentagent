"""Deterministic risk: dual-basis reserve and the portfolio constrainer (§6.1).

Constraints are applied to the *target weight vector* before any order exists.
Per-order enforcement is path dependent — two orders that individually pass can
jointly breach a sector cap, and the outcome then depends on arrival order.

SPECIFIED ORDER OF OPERATIONS. The steps are not commutative, so the sequence
is part of the specification rather than an implementation detail:

    1. capability gate   — drop names the policy does not permit (§5.1 gate 2)
    2. per-name clip     — cap each weight at max_position_pct
    3. sector scale      — scale an offending sector down proportionally
    4. reserve scale     — trim the whole book so settled cash is preserved

Clip-before-scale is deliberate: it lets a high-conviction name keep its full
per-name allowance and takes the sector reduction from the rest, rather than
penalising every name in the sector for one oversized target. Reserve is last
because it is a book-wide monotonic trim and so cannot reintroduce a breach.
"""
from __future__ import annotations

from dataclasses import dataclass

from .policy import Gate, TradeCapabilityPolicy


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
    rejected: tuple[tuple[str, str], ...] = ()   # (symbol, reason)

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
                   sectors: dict[str, str] | None = None,
                   *,
                   capability_policy: TradeCapabilityPolicy | None = None,
                   asset_classes: dict[str, str] | None = None,
                   live: bool = False) -> ConstrainResult:
    """Return feasible weights plus which constraints bound. The caller records
    both requested and authorised values (§6.1).

    capability_policy is gate 2 of the four in §5.1. Passing None skips it and
    is only valid for isolated unit testing — the pipeline always supplies one.
    """
    sectors = sectors or {}
    asset_classes = asset_classes or {}
    binding: list[str] = []
    rejected: list[tuple[str, str]] = []
    w = {s: max(0.0, float(x)) for s, x in target.items() if x and x > 0}

    # 1. capability gate — a disabled instrument never gets a weight
    if capability_policy is not None:
        for sym in list(w):
            asset_class = asset_classes.get(sym, "US_EQUITY")
            if not capability_policy.allows(
                    gate=Gate.RISK_CONSTRAINER, live=live, symbol=sym,
                    asset_class=asset_class, side="BUY", funding="SETTLED_CASH"):
                del w[sym]
                rejected.append((sym, f"capability:{asset_class}"))
                binding.append(f"capability:{sym}")

    # 2. per-name clip
    cap = policy.max_position_pct / 100.0
    for s, x in list(w.items()):
        if x > cap:
            w[s] = cap
            binding.append(f"max_position:{s}")

    # 3. sector scale
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

    # 4. reserve scale
    reserve = required_reserve(p, policy)
    cash = investable_cash(p, policy)
    gross = sum(w.values()) * p.nlv
    if gross > cash:
        k = (cash / gross) if gross > 0 else 0.0
        for s in w:
            w[s] *= k
        binding.append("settled_cash_reserve")

    w = {s: x for s, x in w.items() if x > 1e-9}

    # Post-conditions — one per cap, defence in depth behind the arithmetic.
    assert sum(w.values()) * p.nlv <= cash + 1e-6, "reserve breached"
    assert all(x <= cap + 1e-9 for x in w.values()), "position cap breached"
    for sector, names in by_sector.items():
        total = sum(w.get(s, 0.0) for s in names)
        assert total <= sector_cap + 1e-9, f"sector cap breached: {sector}"

    return ConstrainResult(weights=w, binding=tuple(binding),
                           investable_cash=cash, required_reserve=reserve,
                           rejected=tuple(rejected))
