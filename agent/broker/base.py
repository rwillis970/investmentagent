"""The broker swap seam (§1.2), and capability gate 4 (§5.1).

Everything above this interface — strategy, risk, holding, approval, audit — is
broker agnostic. Adding a live broker means implementing `_submit_impl`,
`_cancel_impl` and the read methods, and nothing else.

Gate 4 lives in `BrokerAdapter.submit`/`cancel` rather than in each concrete
adapter, so a new adapter INHERITS the backstop instead of having to remember
it. Concrete adapters implement `_submit_impl`/`_cancel_impl`, which are only
ever reached after the gate has passed and — in live mode, for submit — after
an approval token has been consumed.

CLOSING THE PIPELINE-BYPASS GAP (§8.3). There used to be a keyword-argument
form of `submit` (symbol=..., side=..., qty=..., ...) that let any caller skip
straight to the broker, bypassing `Gatekeeper.stage` — and with it, reserve,
holding eligibility, the day-trade guard, and position/sector caps. That form
is gone. `cancel` used to be abstract, took a bare `client_order_id`, and
reached broker state with no signature check and no gate 4 -- the same class
of hole, closed the same way:

  1. `submit` and `cancel` are the adapter's ONLY public write surface — both
     concrete, non-abstract methods on this base class. `__init_subclass__`
     below refuses to let a subclass override either one, or define any other
     public method not on an explicit, declared allowlist.
  2. `_submit_impl`/`_cancel_impl` — what a concrete adapter implements — each
     take a `StagedOrder` (the Gatekeeper-signed token) as their only
     parameter, and MUST verify it independently before doing anything, via
     `self._verify_staged_or_raise(...)`. This is deliberately redundant with
     the verification `submit`/`cancel` already do: it means that even a
     hypothetical future method that called `_submit_impl`/`_cancel_impl`
     directly, skipping the public wrapper, still could not act without a
     validly-signed StagedOrder.

Account posture is DETECTED from the broker, never declared in config: config
may assert a posture, and a mismatch halts trading rather than proceeding on an
assumption.

MULTI-ACCOUNT ADDENDUM: one adapter instance exists per account_id, each
holding its own `BrokerCredentials` (a reference, never the secret itself).
`submit`/`cancel` refuse a StagedOrder whose account_id doesn't match this
adapter's own.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from enum import Enum

from ..accounts import BrokerCredentials, CrossAccountError
from ..approval import ApprovalToken, order_fingerprint
from ..pipeline import StagedOrder
from ..policy import Gate, TradeCapabilityPolicy


class AccountPosture(str, Enum):
    CASH = "CASH"
    MARGIN_UNDER_25K = "MARGIN_UNDER_25K"
    MARGIN_OVER_25K = "MARGIN_OVER_25K"
    UNKNOWN = "UNKNOWN"


PDT_EQUITY_THRESHOLD = Decimal("25000")


class AdapterError(Exception):
    pass


class CapabilityPolicyUnset(AdapterError):
    """No policy attached. Fail safe: no policy means no order, not any order."""


class MissingApproval(AdapterError):
    pass


class StagingKeyUnset(AdapterError):
    """No staging key attached. Fail safe: cannot verify a StagedOrder without
    the Gatekeeper's key, so refuse rather than trust an unverifiable order."""


class StagingForged(AdapterError):
    """A StagedOrder's signature is missing or does not match its fields.

    NOT a claim of malicious intent — see the docstring on `sign_staged_order`
    in agent/pipeline.py. This means either the order was constructed without
    going through `Gatekeeper.stage`, or one of its fields was altered after
    staging (including via `dataclasses.replace`, which happily builds a new
    frozen instance with a now-stale signature)."""


@dataclass(frozen=True)
class AccountSnapshot:
    account_id: str
    equity: Decimal
    cash: Decimal
    settled_cash: Decimal
    unsettled_cash: Decimal
    buying_power: Decimal
    multiplier: Decimal              # 1.0 = cash account
    # `None` means the broker did not report this at all -- genuinely
    # UNKNOWN, not "confirmed false"/"confirmed zero". See
    # agent.broker.alpaca.AlpacaPaperAdapter.account() (§13 probe,
    # 2026-07-27): a real Alpaca cash account omits both fields entirely on
    # an account with no PDT history, and Appendix E's fail-safe-to-NO-TRADE
    # forbids silently inventing a concrete value for an absent
    # safety-relevant field. `SimulatorBroker` always knows both (never
    # `None`) because it tracks day trades itself.
    pattern_day_trader: bool | None
    day_trade_count: int | None
    fetched_at: datetime


@dataclass(frozen=True)
class Position:
    account_id: str
    symbol: str
    qty: Decimal
    avg_price: Decimal
    market_value: Decimal


@dataclass(frozen=True)
class Execution:
    """One broker-reported fill INCREMENT -- not an order snapshot. This is
    what `fills()` returns and what `agent.fill_sync.sync_fills` turns into
    ledger `Fill` records, one per increment (never one per order, never
    only at terminal status -- see that module's docstring).

    `execution_id` must be STABLE and per-execution: re-polling an
    unchanged execution must yield the same id, so `sync_fills` can no-op
    on it. `qty`/`price` are THIS increment's own quantity and the price it
    occurred at -- not cumulative and not a running average.
    `cum_qty` is the cumulative filled quantity as of this increment,
    reported separately because some broker APIs (Alpaca's `/v2/orders`)
    only expose the cumulative figure directly; `fills()` implementations
    that source per-execution records (Alpaca's Account Activities
    `FILL`/`TradeActivity`) report both from the same underlying record."""
    execution_id: str
    account_id: str
    client_order_id: str
    symbol: str
    side: str
    qty: Decimal
    price: Decimal
    cum_qty: Decimal
    filled_at: datetime


@dataclass(frozen=True)
class BrokerOrder:
    account_id: str
    client_order_id: str
    broker_order_id: str | None
    symbol: str
    side: str
    qty: Decimal
    order_type: str
    time_in_force: str
    limit_price: Decimal | None
    status: str                     # new|partially_filled|filled|canceled|rejected
    filled_qty: Decimal
    avg_fill_price: Decimal | None
    submitted_at: datetime | None = None
    filled_at: datetime | None = None


# The three of `BrokerOrder.status`'s five canonical values (see the field
# comment above) that will never change again -- reconciled directly against
# `agent.broker.alpaca.STATUS_MAP`, which collapses every raw Alpaca order
# status into exactly these five, and against `SimulatorBroker.open_orders`'s
# own existing membership test (`status in ("new", "partially_filled")`,
# agent/broker/simulator.py), which is this same terminal/non-terminal split
# stated the other way around -- not a new concept, a name for one that
# already existed in two places with no shared constant (found while fixing
# the "nothing ever closes an OrderRecord" gap, 2026-07-30). "new" and
# "partially_filled" are the two NOT here: a partially-filled order can still
# receive further fills, so it is still genuinely open.
TERMINAL_ORDER_STATUSES = frozenset({"filled", "canceled", "rejected"})


def detect_posture(acct: AccountSnapshot) -> AccountPosture:
    if acct.multiplier <= 1.0:
        return AccountPosture.CASH
    return (AccountPosture.MARGIN_OVER_25K
            if acct.equity >= PDT_EQUITY_THRESHOLD
            else AccountPosture.MARGIN_UNDER_25K)


class BrokerAdapter(ABC):
    """Broker state is the source of truth. Local state is a cache.

    One instance per account_id. `credentials`, when given, must belong to
    the same account_id — a mismatch is almost certainly two accounts' wiring
    crossed at construction time, so it fails immediately rather than trading
    account A on account B's credentials."""

    is_live: bool = False
    name: str = "abstract"

    # The adapter's known public surface. `submit` and `cancel` are the only
    # write entrypoints; everything else here is read-only or a shared
    # concrete helper. A concrete subclass that needs additional PUBLIC
    # methods (e.g. a paper simulator's test controls) must declare them
    # explicitly via its own `_extra_public_methods` class attribute.
    _known_public_surface: frozenset[str] = frozenset({
        "account", "positions", "open_orders", "get_by_client_id", "sessions",
        "submit", "cancel", "clock", "posture", "supported_matrix", "fills",
        "attach_capability_policy", "attach_staging_key",
    })

    def __init_subclass__(cls, **kwargs) -> None:
        super().__init_subclass__(**kwargs)

        # 1. submit/cancel may not be overridden. This is the tripwire for
        #    the exact hole this module's docstring describes: Python cannot
        #    forbid the override outright, but it can refuse to let the
        #    subclass exist with one.
        if cls.__dict__.get("submit") is not None:
            raise TypeError(
                f"{cls.__name__} may not define its own submit(); it is the "
                "only gated write entrypoint for new/close orders and "
                "re-implementing it would bypass the HMAC and gate-4 "
                "discipline. Implement _submit_impl instead."
            )
        if cls.__dict__.get("cancel") is not None:
            raise TypeError(
                f"{cls.__name__} may not define its own cancel(); implement "
                "_cancel_impl instead."
            )

        # 2. no undeclared new public methods.
        declared = cls.__dict__.get("_extra_public_methods", frozenset())
        allowed = BrokerAdapter._known_public_surface | set(declared)
        for name, value in cls.__dict__.items():
            if name.startswith("_") or name in allowed:
                continue
            if isinstance(value, property):
                continue
            if callable(value):
                raise TypeError(
                    f"{cls.__name__}.{name} is a new public method not on the "
                    "adapter's known write/read surface. If this is a "
                    "deliberate, non-order-writing addition (a test control, "
                    "say), add its name to this class's _extra_public_methods "
                    "so the addition is explicit. If it writes to the broker, "
                    "it must route through Gatekeeper.stage + submit()/"
                    "cancel() instead of existing as its own method at all."
                )

    def __init__(self, account_id: str,
                 credentials: BrokerCredentials | None = None,
                 capability_policy: TradeCapabilityPolicy | None = None,
                 staging_key: bytes | None = None):
        if credentials is not None and credentials.account_id != account_id:
            raise CrossAccountError(account_id, credentials.account_id,
                                    f"{type(self).__name__}.__init__ credentials")
        self.account_id = account_id
        self._credentials = credentials
        self._capability_policy = capability_policy
        self._staging_key = staging_key

    # -- policy -----------------------------------------------------------
    @property
    def capability_policy(self) -> TradeCapabilityPolicy:
        if self._capability_policy is None:
            raise CapabilityPolicyUnset(
                f"{self.name}: no capability policy attached; refusing to trade"
            )
        return self._capability_policy

    def attach_capability_policy(self, policy: TradeCapabilityPolicy) -> None:
        self._capability_policy = policy

    def attach_staging_key(self, key: bytes) -> None:
        """Wire this adapter to the same key the Gatekeeper signs with. Without
        this, `submit`/`cancel` refuse every StagedOrder — there is no default
        key."""
        self._staging_key = key

    def clock(self) -> datetime:
        return datetime.now(timezone.utc)

    # -- verification, shared by submit(), cancel(), and the concrete impls --
    def _verify_staged_or_raise(self, staged: StagedOrder, *, where: str) -> None:
        if not isinstance(staged, StagedOrder):
            raise AdapterError(
                f"{where} takes a StagedOrder from Gatekeeper.stage; no other "
                "form is accepted"
            )
        if self._staging_key is None:
            raise StagingKeyUnset(
                f"{self.name}: no staging key attached; refusing {where} for "
                "an order whose signature cannot be verified"
            )
        if not staged.verify(self._staging_key):
            raise StagingForged(
                f"{self.name}: StagedOrder signature is missing or does not "
                f"match its fields for client_order_id={staged.client_order_id!r} "
                f"in {where}. Refusing."
            )
        if staged.account_id != self.account_id:
            raise CrossAccountError(self.account_id, staged.account_id,
                                    f"{self.name}.{where}")

    # -- read -------------------------------------------------------------
    @abstractmethod
    def account(self) -> AccountSnapshot: ...

    @abstractmethod
    def positions(self) -> list[Position]: ...

    @abstractmethod
    def open_orders(self) -> list[BrokerOrder]: ...

    @abstractmethod
    def get_by_client_id(self, client_order_id: str) -> BrokerOrder | None:
        """Resolve an ambiguous acknowledgement. Never resubmit to find out."""

    @abstractmethod
    def sessions(self, through: date, count: int = 5) -> list[date]:
        """Trailing trading sessions, for the day-trade window (§4.4).
        Implementations should delegate to `market_calendar.
        trailing_sessions` rather than re-deriving this (weekday-only,
        say) -- see `SimulatorBroker.sessions` for the reference
        implementation. There is meant to be exactly one holiday-aware
        trailing-sessions implementation in this codebase."""

    @abstractmethod
    def fills(self) -> list[Execution]:
        """Every broker-reported fill INCREMENT this adapter can currently
        see -- not deduplicated, not filtered to "new since last call".
        `agent.fill_sync.sync_fills` is what turns this into ledger
        writes, deciding what's new by `execution_id`. Implementations
        should prefer a real per-execution record over a per-order
        cumulative/averaged one where the broker's API offers it (see
        `AlpacaPaperAdapter.fills`, which uses Account Activities rather
        than `/v2/orders`, for exactly this reason)."""

    # -- write --------------------------------------------------------------
    def submit(self, staged: StagedOrder, *,
               approval_token: ApprovalToken | None = None,
               reference_price: float | None = None) -> BrokerOrder:
        """The only way to submit a new order or a close. Takes a
        `StagedOrder` — the output of `Gatekeeper.stage` — and nothing else.
        Do not override this in an adapter; implement `_submit_impl` instead
        (enforced at class-definition time — see `__init_subclass__`)."""
        if staged.side == "CANCEL":
            raise AdapterError(
                "submit() cannot take a CANCEL StagedOrder -- use cancel() "
                "instead."
            )
        self._verify_staged_or_raise(staged, where="submit")

        # Gate 4 — re-derived from the staged order's own fields, independent
        # of the signature check above.
        self.capability_policy.check(
            gate=Gate.ADAPTER, live=self.is_live, symbol=staged.symbol,
            asset_class=staged.asset_class, side=staged.side,
            funding=staged.funding, order_type=staged.order_type,
            session=staged.session, time_in_force=staged.time_in_force,
        )

        if self.is_live:
            if approval_token is None:
                raise MissingApproval(
                    "a live order requires an approval token (§12 criterion 13)"
                )
            price = reference_price if reference_price is not None else staged.limit_price
            if price is None:
                raise MissingApproval(
                    "cannot validate the approved price band without a reference price"
                )
            # Consumed atomically here: a retry, replay or restart cannot reuse it.
            approval_token.consume(
                fingerprint=order_fingerprint(
                    symbol=staged.symbol, side=staged.side, qty=staged.authorized_qty,
                    order_type=staged.order_type, time_in_force=staged.time_in_force,
                    limit_price=staged.limit_price, lot_id=staged.lot_id),
                price=price, now=self.clock(),
            )

        return self._submit_impl(staged)

    def cancel(self, staged: StagedOrder) -> BrokerOrder | None:
        """The only way to cancel an order. Mirrors submit()'s discipline —
        verify signature, verify account_id, re-derive gate-4 capability from
        the staged order's own fields — but deliberately does NOT require an
        approval token even in live mode: a risk limit, a stale holding fact
        or a missing approval must never be the reason an order stays
        resting in the market.

        Do not override this in an adapter; implement `_cancel_impl` instead
        (enforced at class-definition time — see `__init_subclass__`).
        """
        if not isinstance(staged, StagedOrder) or staged.side != "CANCEL":
            raise AdapterError(
                "cancel() takes a StagedOrder staged with side=CANCEL from "
                "Gatekeeper.stage; no other form is accepted"
            )
        self._verify_staged_or_raise(staged, where="cancel")

        self.capability_policy.check(
            gate=Gate.ADAPTER, live=self.is_live, symbol=staged.symbol,
            asset_class=staged.asset_class, side=staged.side,
            funding=staged.funding, order_type=staged.order_type,
            session=staged.session, time_in_force=staged.time_in_force,
        )

        return self._cancel_impl(staged)

    @abstractmethod
    def _submit_impl(self, staged: StagedOrder) -> BrokerOrder:
        """MUST call `self._verify_staged_or_raise(staged, where="_submit_impl")`
        before doing anything else, independent of submit() having already
        done so. MUST be idempotent on `staged.client_order_id`: submitting
        the same id twice returns the existing order rather than creating a
        second one."""

    @abstractmethod
    def _cancel_impl(self, staged: StagedOrder) -> BrokerOrder | None:
        """MUST call `self._verify_staged_or_raise(staged, where="_cancel_impl")`
        before doing anything else."""

    # -- shared -----------------------------------------------------------
    def posture(self) -> AccountPosture:
        return detect_posture(self.account())

    def supported_matrix(self) -> dict[str, list[str]]:
        """Empirically discovered capabilities (§13). The plan requires probing
        the live API rather than trusting documentation."""
        return {}
