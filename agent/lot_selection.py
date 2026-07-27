"""Lot disposal / tax-lot selection (Commit 4, §4.1 divergence note).

WHY THIS MODULE EXISTS. An internal `lot_id` on our side identifies which
lot OUR strategy *intends* to reduce when it stages a SELL. It does not
control which lot Alpaca actually treats as sold -- Alpaca matches sells
against its own book, using its own default disposal method, and offers no
per-order lot designation. If code anywhere (in particular
`agent.holding.sellable_qty`, the live minimum-hold gate) reasons about the
intended lot instead of the lot the broker will actually consume, it can
believe a seasoned, hold-eligible lot was sold while the broker actually
consumed a fresh one that hadn't cleared its minimum hold -- silently
defeating the gate the minimum-hold policy exists to enforce (§4.2). This
module is the one place that answers "what order will the broker actually
consume lots in," so nothing else has to guess.

WHAT ALPACA'S ACTUAL DEFAULT METHOD IS, AND WHETHER LOT DESIGNATION EXISTS
(researched before writing any code here, per instruction -- not assumed):

1. The Margin and Customer Agreement
   (https://files.alpaca.markets/disclosures/library/AcctAppMarginAndCustAgmt.pdf,
   version V25.2026.06, retrieved 2026-07-27) was read in full (1,397 lines
   of extracted text). Its only relevant clause is §39 "Tax Reporting; Tax
   Withholding" (page 22): cost basis information for sale transactions
   "will be reported to the Internal Revenue Service in accordance with
   applicable law." It names no disposal method, does not mention FIFO,
   LIFO, HIFO or specific identification, and does not describe any
   lot-designation right. THE CUSTOMER AGREEMENT DOES NOT ESTABLISH THE
   DISPOSAL METHOD. This is reported here rather than papered over, per
   instruction to say so if the agreement doesn't answer it.

2. Alpaca's own product documentation does answer it. "Position Average
   Entry Price Calculation"
   (https://docs.alpaca.markets/us/docs/position-average-entry-price-calculation,
   retrieved 2026-07-27) states plainly, under "Which Method is Alpaca
   Using?": Weighted Average is used for INTRADAY positions (same-day
   buys), and Compressed FIFO is used for END-OF-DAY positions. Compressed
   FIFO first compresses each day's same-day buys into one weighted-average
   lot (via the beginning-of-day job), then consumes those day-aggregates
   oldest-day-first on a later sell. No specific-identification, HIFO, LIFO
   or tax-optimised alternative is documented anywhere on that page or
   linked from it.

3. No API-level lot designation exists. The order-submission reference
   (https://docs.alpaca.markets/us/docs/orders-at-alpaca) lists the actual
   request fields: symbol, qty/notional, side, type, time_in_force,
   limit_price, stop_price, trail_price/trail_percent, extended_hours,
   client_order_id, order_class and bracket legs. There is no lot id or
   tax-lot parameter of any kind. This absence is corroborated (not proven
   on its own, since an API reference can't prove a negative) by Alpaca's
   own GitHub issue tracker -- alpacahq/Alpaca-API#213, "Selling from a
   specific lot": "Unfortunately, it's not something we can handle on our
   end" -- and by multiple Alpaca community forum threads spanning
   2020-2024 ("Selling specific lots of shares", "Is Alpaca FIFO or LIFO?",
   "LIFO during Intraday trading", "Selecting tax lots at year end?", the
   last of which states plainly: "My understanding is alpaca is fixed to
   FIFO"), all independently describing the same fixed-FIFO, no-designation
   behaviour with no contradicting report found.

CONCLUSION: Alpaca's confirmed actual default is FIFO (Compressed FIFO
across days, Weighted Average within a day), and no other method is
selectable through the API. `LotSelectionMethod.BROKER_FIFO` is therefore
the only method this module implements. The rest are enumerated, per
instruction, and refuse to run rather than silently approximate.

KNOWN, RECORDED APPROXIMATION THIS MODULE DOES NOT CLOSE. Our own `Lot` is
one lot per BUY fill; Alpaca's same-day weighted-average compression across
multiple same-day buys is not modelled here. `disposal_order` below
approximates Alpaca's method as plain fill-time FIFO across ALL open lots,
regardless of same-day grouping. For the pilot's shape (no same-day
pyramiding into a single symbol expected) this gap is not expected to bind,
but it is a real, named approximation -- not an exact replica of Alpaca's
method -- and is recorded here rather than silently assumed away. See
docs/architecture.md §4.1 for the corresponding note.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class LotSelectionMethod(Enum):
    BROKER_FIFO = "broker_fifo"
    SPECIFIC_IDENTIFICATION = "specific_identification"
    HIFO = "hifo"
    LIFO = "lifo"
    TAX_OPTIMIZED = "tax_optimized"


# The only method Alpaca has confirmed it uses, and the only one that needs
# no API-level lot designation to honour (see module docstring). Every other
# enum member exists so the vocabulary is complete, not because it is usable
# against this broker today.
SUPPORTED_METHODS = frozenset({LotSelectionMethod.BROKER_FIFO})


class UnsupportedLotSelectionPolicy(Exception):
    """Raised by `disposal_order` for any method other than BROKER_FIFO, and
    by the registry for an unknown/conflicting version. There is
    deliberately no fallback path to a guessed ordering: a method Alpaca
    hasn't confirmed it will honour must not be silently approximated as
    FIFO, because being wrong about that is exactly the failure mode this
    module exists to prevent."""


@dataclass(frozen=True)
class LotSelectionPolicy:
    """Versioned, like `agent.holding.HoldingPolicy` -- a policy's meaning
    is frozen once registered, so a policy version referenced by an old
    record always resolves to what it meant then."""
    version: str
    method: LotSelectionMethod


class LotSelectionPolicyRegistry:
    def __init__(self, policies=()):
        self._by_version: dict[str, LotSelectionPolicy] = {}
        for p in policies:
            self.register(p)

    def register(self, policy: LotSelectionPolicy) -> LotSelectionPolicy:
        existing = self._by_version.get(policy.version)
        if existing is not None and existing != policy:
            raise UnsupportedLotSelectionPolicy(
                f"lot selection policy {policy.version} is already registered "
                "with different values; policy versions are immutable"
            )
        self._by_version[policy.version] = policy
        return policy

    def get(self, version: str) -> LotSelectionPolicy:
        try:
            return self._by_version[version]
        except KeyError as exc:
            raise UnsupportedLotSelectionPolicy(
                f"unknown lot selection policy version {version!r}"
            ) from exc


# Alpaca's confirmed actual default (see module docstring for citations).
# This is a specific, cited fact about one broker, not a project-wide
# assumption -- a future broker swap (agent.broker.BrokerAdapter is the
# swap seam) must supply its OWN confirmed policy rather than silently
# inherit this one.
ALPACA_DEFAULT_POLICY = LotSelectionPolicy(
    version="alpaca-2026-07", method=LotSelectionMethod.BROKER_FIFO,
)


def disposal_order(policy: LotSelectionPolicy, lots):
    """Return `lots` (anything with `.lot_id` and `.opened_at`) in the order
    the broker will actually dispose of them.

    Only BROKER_FIFO is implemented. The others raise
    `UnsupportedLotSelectionPolicy` rather than being approximated, per the
    module docstring. Sorted by `(opened_at, lot_id)` -- the lot_id
    tiebreak makes the order deterministic even when two lots share an
    `opened_at` instant, rather than silently depending on whatever order
    the caller happened to pass them in.
    """
    if policy.method not in SUPPORTED_METHODS:
        raise UnsupportedLotSelectionPolicy(
            f"{policy.method.value} is not implemented: Alpaca has not "
            "confirmed any API-level lot designation exists (see "
            "agent.lot_selection module docstring for citations), so this "
            "method cannot be honoured against the actual broker and must "
            "not be silently approximated as FIFO"
        )
    return sorted(lots, key=lambda l: (l.opened_at, l.lot_id))
