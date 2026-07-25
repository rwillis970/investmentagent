"""Trade capability policy and the four-gate check (§5).

No module anywhere else in the codebase may hardcode an instrument assumption.
Everything asks this policy, and the policy version is pinned in the run
manifest so a decision can be replayed against the rules that governed it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum


class CapabilityStatus(IntEnum):
    DISABLED = 0
    RESEARCH_ONLY = 1
    PAPER_ONLY = 2
    APPROVAL_REQUIRED = 3
    PRODUCTION_ALLOWED = 4


class Gate(IntEnum):
    """Where the check happened — recorded on every rejection (§5.1)."""
    UNIVERSE = 1
    RISK_CONSTRAINER = 2
    PRE_SUBMIT = 3
    ADAPTER = 4


class PolicyViolation(Exception):
    def __init__(self, gate: Gate, dimension: str, value: str, status: CapabilityStatus):
        self.gate, self.dimension, self.value, self.status = gate, dimension, value, status
        super().__init__(
            f"{dimension}={value} is {status.name}; blocked at gate {gate.name}"
        )


@dataclass(frozen=True)
class TradeCapabilityPolicy:
    version: str
    asset_class: dict[str, CapabilityStatus] = field(default_factory=dict)
    side: dict[str, CapabilityStatus] = field(default_factory=dict)
    funding: dict[str, CapabilityStatus] = field(default_factory=dict)
    order_type: dict[str, CapabilityStatus] = field(default_factory=dict)
    session: dict[str, CapabilityStatus] = field(default_factory=dict)
    time_in_force: dict[str, CapabilityStatus] = field(default_factory=dict)
    symbol_blocklist: frozenset[str] = frozenset()

    _DIMENSIONS = ("asset_class", "side", "funding", "order_type",
                   "session", "time_in_force")

    def status(self, dimension: str, value: str) -> CapabilityStatus:
        table = getattr(self, dimension, None)
        if table is None:
            raise KeyError(f"unknown capability dimension {dimension!r}")
        # Default deny. An unlisted value is DISABLED, never permitted.
        return table.get(str(value).upper(), CapabilityStatus.DISABLED)

    def required(self, live: bool) -> CapabilityStatus:
        return (CapabilityStatus.APPROVAL_REQUIRED if live
                else CapabilityStatus.PAPER_ONLY)

    def check(self, *, gate: Gate, live: bool, symbol: str | None = None,
              **dimensions: str) -> None:
        """Raise PolicyViolation unless every supplied dimension permits the
        action at this mode. Called at all four gates with the same arguments."""
        if symbol and symbol.upper() in self.symbol_blocklist:
            raise PolicyViolation(gate, "symbol", symbol, CapabilityStatus.DISABLED)
        floor = self.required(live)
        for dimension, value in dimensions.items():
            if dimension not in self._DIMENSIONS:
                raise KeyError(f"unknown capability dimension {dimension!r}")
            st = self.status(dimension, value)
            if st < floor:
                raise PolicyViolation(gate, dimension, str(value), st)

    def allows(self, **kwargs) -> bool:
        try:
            self.check(**kwargs)
            return True
        except PolicyViolation:
            return False


def initial_policy(version: str = "v1") -> TradeCapabilityPolicy:
    """Appendix E — the initial safety boundary, as data."""
    P, D, PO = (CapabilityStatus.PRODUCTION_ALLOWED, CapabilityStatus.DISABLED,
                CapabilityStatus.PAPER_ONLY)
    return TradeCapabilityPolicy(
        version=version,
        asset_class={"US_EQUITY": P, "ETF": P, "OPTIONS": D, "CRYPTO": D,
                     "FUTURES": D, "FOREX": D, "OTC": D, "SHORT_SELLING": D,
                     "MARGIN": D},
        side={"BUY": P, "SELL": P, "SELL_SHORT": D, "BUY_TO_COVER": D},
        funding={"SETTLED_CASH": P, "MARGIN": D, "UNSETTLED_CASH": D},
        order_type={"LIMIT": P, "MARKET": P, "STOP": P, "STOP_LIMIT": P,
                    "TRAILING_STOP": PO},
        session={"REGULAR": P, "EXTENDED": D, "OVERNIGHT": D},
        time_in_force={"DAY": P, "GTC": D, "IOC": D, "FOK": D},
    )
