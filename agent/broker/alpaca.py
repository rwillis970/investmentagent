"""Alpaca paper adapter (§1.2, §11 Day 10).

Moved ahead of the collectors: Alpaca is one API for both paper and live
(§1.2's recommendation), so the HTTP/mapping logic in this module is written
generically enough to serve a future live adapter too -- but only the PAPER
half is actually built and enabled here, per this unit's explicit
constraint. No `AlpacaLiveAdapter` exists in this codebase yet; Day 10's
remaining scope (re-authentication, pre-submit re-check for a real live
adapter) is separate, later work.

STRUCTURAL ISOLATION -- MIRRORS `agent.secrets_provider.SecretsProvider`.
`BASE_URL`, `is_live` and `name` are fixed CLASS attributes on
`AlpacaPaperAdapter`, not constructor arguments -- there is no flag on this
class that could be flipped at runtime to reach the live endpoint. A live
adapter, when built, will be its own class with its own `BASE_URL`; it will
not be this class constructed with `is_live=True`. This is the same
"bound at construction, not a runtime argument" isolation `SecretsProvider`
uses for mode, and for the same reason: the thing worth preventing is a
single object that could be pointed at either environment depending on how
it's called.

CREDENTIALS. `BrokerCredentials.key_id` is used directly (it is a public-ish
identifier, not the secret -- see accounts.py's own docstring). The actual
API secret is resolved via `SecretsProvider.resolve(credentials.secret_ref)`
FRESH ON EVERY REQUEST, never cached on `self` -- "resolve at point of use,"
per this unit's constraint, and per secrets_provider.py's own "resolve at
the point of use" framing. At construction, this adapter refuses to be
built at all if the given `secrets_provider`'s mode does not match what this
class expects (`PAPER`) -- the same kind of immediate, construction-time
mismatch check `BrokerAdapter.__init__` already does for `credentials.
account_id`.

CASH FIELD MAPPING -- REPORTED HONESTLY, NOT GUESSED. Alpaca's `/v2/account`
response (confirmed against `alpaca-py`'s own `TradeAccount` model, fetched
directly from https://github.com/alpacahq/alpaca-py) has NO field that
splits settled from unsettled cash. It has `cash` (a single combined
balance), `pending_transfer_in`/`pending_transfer_out` (cash pending
TRANSFER -- deposits/withdrawals, not trade settlement), and several
buying-power variants, but nothing that answers "how much of `cash` is
actually available to spend today because it's already settled." This
adapter maps:

    settled_cash = float(data["cash"])   -- the closest available figure
    unsettled_cash = 0.0                 -- ALWAYS, because there is nothing
                                            to compute it from

This is an APPROXIMATION, not an exact mapping, and it is very likely wrong
whenever a recent sale has proceeds still pending T+1 settlement -- Alpaca's
`cash` may or may not already reflect that pending amount (this was not
possible to confirm from the model alone), and this adapter has no way to
tell. Concretely, this means `agent.reconciliation.reconcile_settled_cash`
(built assuming a local ledger and a broker snapshot should agree exactly)
may see spurious mismatches, or worse, may fail to notice a real one, right
around a settlement event. This is a genuine, load-bearing gap: before this
adapter's `account()` is trusted for real reconciliation, it needs
verification against a real paper account's actual behaviour across a T+1
settlement, or a better source (e.g. the Account Activities endpoint, which
lists settlement-relevant non-trade activity) -- neither was in scope here.

STATUS MAPPING -- see `STATUS_MAP` below for the full table and which of
Alpaca's seventeen `OrderStatus` values do not map cleanly onto this
codebase's five-state vocabulary (new/partially_filled/filled/canceled/
rejected, per `agent.broker.base.BrokerOrder`).

TIMEOUTS AND RETRIES -- see `Config.broker_http_timeout_seconds`/
`broker_http_max_retries` (agent/config.py, §9.1). Retries apply ONLY to
reads (`account`, `positions`, `open_orders`, `get_by_client_id`) -- they
have no side effects, so a bounded retry on a transport failure is safe.
WRITES (`_submit_impl`/`_cancel_impl`) NEVER RETRY, regardless of this
setting: seeAmbiguousOrderState below for the dangerous case this unit
specifically asked about -- a submit that times out after the order may
have already reached Alpaca.

AMBIGUOUS WRITES. `_submit_impl` and `_cancel_impl` each make exactly ONE
HTTP attempt. If that attempt raises ANY `TransportError` (a timeout, or
any other transport-level failure), this adapter does NOT retry and does
NOT assume the write failed -- it raises `AmbiguousOrderState`, naming the
`client_order_id`, and the message says explicitly: resolve via
`get_by_client_id` before doing anything else, never resubmit. This is
deliberately uniform across timeout vs. other transport errors, even though
a plain connection-refused failure (nothing sent yet) is arguably less
ambiguous than a timeout (request possibly sent, response lost) --
distinguishing them reliably would need lower-level socket instrumentation
this stdlib-only transport does not have, and treating a genuinely-safe
case as ambiguous costs one extra read; treating a genuinely-ambiguous case
as safe risks a duplicate live order. The conservative direction is the
only one available cheaply, so it is applied uniformly.

IDEMPOTENCY ON client_order_id -- REUSING ALPACA'S OWN, NOT REIMPLEMENTING
IT. `_submit_impl` checks `get_by_client_id` FIRST and returns the existing
order immediately if found, mirroring `SimulatorBroker._submit_impl`'s own
in-memory short-circuit exactly (`BrokerAdapter._submit_impl`'s contract:
"submitting the same id twice returns the existing order rather than
creating a second one"). If a race still gets a duplicate-client_order_id
rejection past that check (Alpaca returns 422), this adapter resolves it by
looking the order up again rather than treating 422 as a hard failure --
distinguishing "already exists" from a genuine rejection by re-querying,
not by parsing Alpaca's error message text (not a stable contract to
depend on).
"""
from __future__ import annotations

import re
from datetime import date, datetime, timezone

from ..accounts import BrokerCredentials
from ..pipeline import StagedOrder
from ..policy import TradeCapabilityPolicy
from .. import market_calendar
from ..secrets_provider import SecretsProvider
from .base import AccountSnapshot, AdapterError, BrokerAdapter, BrokerOrder, Position
from .transport import Transport, TransportError, UrllibTransport

_EXPECTED_SECRETS_MODE = "PAPER"

# Alpaca's OrderStatus has 17 values (confirmed against alpaca-py's
# alpaca.trading.enums.OrderStatus, fetched directly from
# https://github.com/alpacahq/alpaca-py -- Alpaca's own docs site renders
# this table client-side and could not be fetched as static text). This
# codebase's BrokerOrder vocabulary has five (agent/broker/base.py): new,
# partially_filled, filled, canceled, rejected. Nine of the seventeen map
# exactly or unambiguously; eight do NOT map cleanly and are folded into the
# closest of the five as a documented judgment call, not a confident
# mapping -- named individually below.
STATUS_MAP: dict[str, str] = {
    # -- Exact matches --
    "new": "new",
    "partially_filled": "partially_filled",
    "filled": "filled",
    "canceled": "canceled",
    "rejected": "rejected",
    # -- Still open/working, no fill yet: folded into "new" --
    "accepted": "new",              # acknowledged, not yet routed to the exchange
    "pending_new": "new",           # accepted by Alpaca, not yet sent onward
    "accepted_for_bidding": "new",  # auction-eligible (MOO/LOO/MOC/LOC), not yet executed
    "pending_cancel": "new",        # cancel REQUESTED but not yet confirmed -- still
                                     # open; treating this as canceled early would be
                                     # wrong if the cancel itself is rejected
    "pending_replace": "new",       # a replace request pending -- REPLACE is not
                                     # implemented anywhere this codebase submits from
                                     # (Gatekeeper.stage raises NotImplementedError for
                                     # it), so this should be unreachable via our own orders
    # -- Terminal and unfilled, but NOT a cancellation WE asked for: folded
    #    into "canceled" as the closest terminal-unfilled bucket. This
    #    collapses a real distinction (the order's window elapsed vs.
    #    someone/something cancelled it) that an operator might want kept
    #    separate -- it is not preserved here.
    "done_for_day": "canceled",     # a DAY order's session ended, unfilled
    "expired": "canceled",          # past its own expires_at
    "replaced": "canceled",         # superseded by a replacement order (PATCH) --
                                     # shouldn't occur from this codebase's own orders
    # -- Genuinely ambiguous. Does NOT map cleanly onto any of the five;
    #    folded into "new" (i.e. "not final") as the least-wrong choice,
    #    but this is a named judgment call, not a confident mapping:
    "pending_review": "new",   # held for compliance review; could sit indefinitely
                               # with no fill and no explicit rejection
    "stopped": "new",          # exchange-guaranteed partial execution pending --
                               # unusual for equities, no clean equivalent here
    "suspended": "new",        # suspended from trading; not final, but not really "new"
    "calculated": "new",       # completed pending final price calculation -- closer in
                               # spirit to "filled" but not confirmed filled
    "held": "new",             # held for a corporate action or manual review
}


class AlpacaError(AdapterError):
    """Base for Alpaca-adapter-specific failures."""


class AmbiguousOrderState(AlpacaError):
    """A write's outcome could not be determined: the transport call itself
    failed (timeout or otherwise), not Alpaca returning a normal response.
    The order may or may not have been created/cancelled at Alpaca. Resolve
    via `get_by_client_id` before doing anything else -- never resubmit or
    retry blindly. See module docstring."""


class UnsupportedOrderShape(AlpacaError):
    """A fetched order does not fit this codebase's qty-based, five-status
    `BrokerOrder` model -- e.g. a notional-only order with neither `qty`
    nor `filled_qty`, or a status `STATUS_MAP` has no entry for. This
    adapter's own `_submit_impl` never creates such an order; seeing one
    means something else (e.g. the Alpaca dashboard, used directly on the
    same paper account) created it outside this system."""


def _parse_ts(raw: str | None) -> datetime | None:
    """Alpaca timestamps are RFC3339 UTC (e.g. '2026-07-20T13:00:00Z' or
    with up to 9 fractional digits). `datetime.fromisoformat` accepts at
    most 6 fractional digits and (before 3.11) no bare 'Z' suffix -- this
    normalizes both before parsing, and always attaches UTC tzinfo
    explicitly (this codebase requires timezone-aware datetimes throughout;
    see agent/mode_store.py's own naive-datetime rejection)."""
    if not raw:
        return None
    s = raw[:-1] if raw.endswith("Z") else raw
    s = re.sub(r"\+00:00$", "", s)
    if "." in s:
        head, frac = s.split(".", 1)
        s = f"{head}.{frac[:6]}"
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)


class AlpacaPaperAdapter(BrokerAdapter):
    """Alpaca's paper-trading API (§1.2, §11 Day 10). See module docstring
    for the cash-mapping and status-mapping judgment calls, and for why
    only the paper half of "one API, paper and live" is built here."""

    is_live = False
    name = "alpaca_paper"
    BASE_URL = "https://paper-api.alpaca.markets"

    def __init__(self, *, account_id: str, credentials: BrokerCredentials | None,
                secrets_provider: SecretsProvider,
                capability_policy: TradeCapabilityPolicy | None = None,
                staging_key: bytes | None = None,
                transport: Transport | None = None,
                http_timeout_seconds: float = 10.0,
                http_max_retries: int = 2):
        if credentials is None:
            raise AlpacaError(f"{self.name}: credentials are required")
        super().__init__(account_id, credentials, capability_policy, staging_key)
        if secrets_provider.mode != _EXPECTED_SECRETS_MODE:
            raise AlpacaError(
                f"{self.name} is bound to mode={_EXPECTED_SECRETS_MODE!r}, but "
                f"was given a secrets_provider bound to mode={secrets_provider.mode!r}. "
                "This is exactly the mismatch structural isolation exists to "
                "catch at construction, before a single request is made."
            )
        self._secrets = secrets_provider
        self._transport = transport or UrllibTransport()
        self._timeout = http_timeout_seconds
        self._max_retries = http_max_retries

    # -- transport plumbing -------------------------------------------------
    def _headers(self) -> dict[str, str]:
        # Resolved fresh on every call -- never cached on self. See module
        # docstring's CREDENTIALS section.
        return {
            "APCA-API-KEY-ID": self._credentials.key_id,
            "APCA-API-SECRET-KEY": self._secrets.resolve(self._credentials.secret_ref),
            "Content-Type": "application/json",
        }

    def _request(self, method: str, path: str, *, params: dict | None = None,
                json_body: dict | None = None, retryable: bool) -> tuple[int, dict]:
        """`retryable` distinguishes reads (safe to retry, bounded by
        `http_max_retries`) from writes (never retried -- see module
        docstring's AMBIGUOUS WRITES section). Exactly one attempt when
        `retryable` is False."""
        url = f"{self.BASE_URL}{path}"
        attempts = (self._max_retries + 1) if retryable else 1
        last_exc: TransportError | None = None
        for _ in range(attempts):
            try:
                return self._transport.request(
                    method, url, headers=self._headers(), params=params,
                    json_body=json_body, timeout=self._timeout)
            except TransportError as exc:
                last_exc = exc
                if not retryable:
                    raise
        assert last_exc is not None
        raise last_exc

    # -- read ---------------------------------------------------------------
    def account(self) -> AccountSnapshot:
        _, data = self._request("GET", "/v2/account", retryable=True)
        return AccountSnapshot(
            account_id=self.account_id,
            equity=float(data["equity"]),
            cash=float(data["cash"]),
            # APPROXIMATE, not exact -- see module docstring's CASH FIELD
            # MAPPING section. Alpaca's /v2/account has no settled/
            # unsettled split.
            settled_cash=float(data["cash"]),
            unsettled_cash=0.0,
            buying_power=float(data["buying_power"]),
            multiplier=float(data["multiplier"]),
            pattern_day_trader=bool(data.get("pattern_day_trader", False)),
            day_trade_count=int(data.get("daytrade_count", 0)),
            fetched_at=datetime.now(timezone.utc),
        )

    def positions(self) -> list[Position]:
        _, data = self._request("GET", "/v2/positions", retryable=True)
        out = []
        for p in data:
            qty = float(p["qty"])
            if p.get("side") == "short":
                # Shorting is DISABLED at the capability layer (Appendix E)
                # for this pilot regardless -- if one somehow existed, it
                # must be reported faithfully (broker state is the source
                # of truth), not coerced positive, so reconciliation can
                # flag it as the anomaly it would be.
                qty = -qty
            avg_price = float(p["avg_entry_price"])
            mv_raw = p.get("market_value")
            market_value = float(mv_raw) if mv_raw not in (None, "") else qty * avg_price
            out.append(Position(account_id=self.account_id, symbol=p["symbol"],
                               qty=qty, avg_price=avg_price, market_value=market_value))
        return out

    def open_orders(self) -> list[BrokerOrder]:
        _, data = self._request("GET", "/v2/orders", params={"status": "open"}, retryable=True)
        return [self._to_broker_order(o) for o in data]

    def get_by_client_id(self, client_order_id: str) -> BrokerOrder | None:
        status, data = self._request(
            "GET", "/v2/orders:by_client_order_id",
            params={"client_order_id": client_order_id}, retryable=True)
        if status == 404:
            return None
        return self._to_broker_order(data)

    def _to_broker_order(self, o: dict) -> BrokerOrder:
        qty_raw = o.get("qty")
        if qty_raw in (None, ""):
            filled = o.get("filled_qty")
            if filled in (None, ""):
                raise UnsupportedOrderShape(
                    f"order {o.get('client_order_id')!r} has no qty and no "
                    "filled_qty -- likely a notional-only order, which this "
                    "codebase's BrokerOrder cannot represent (it is qty-based "
                    "throughout; StagedOrder.authorized_qty is always a qty, "
                    "never a notional). This adapter's own submit() never "
                    "creates a notional order, so this can only happen if "
                    "something else placed one on this account outside this "
                    "system."
                )
            qty = float(filled)
        else:
            qty = float(qty_raw)

        alpaca_status = o["status"]
        status = STATUS_MAP.get(alpaca_status)
        if status is None:
            raise UnsupportedOrderShape(
                f"order {o.get('client_order_id')!r}: unrecognised Alpaca "
                f"order status {alpaca_status!r} -- STATUS_MAP has no entry "
                "for it (see agent/broker/alpaca.py)"
            )

        order_type = (o.get("type") or o.get("order_type") or "").upper()
        limit_price = o.get("limit_price")
        avg_fill_price = o.get("filled_avg_price")
        return BrokerOrder(
            account_id=self.account_id,
            client_order_id=o["client_order_id"],
            broker_order_id=o.get("id"),
            symbol=o["symbol"],
            side=o["side"].upper(),
            qty=qty,
            order_type=order_type,
            time_in_force=o["time_in_force"].upper(),
            limit_price=float(limit_price) if limit_price not in (None, "") else None,
            status=status,
            filled_qty=float(o.get("filled_qty") or 0.0),
            avg_fill_price=float(avg_fill_price) if avg_fill_price not in (None, "") else None,
            submitted_at=_parse_ts(o.get("submitted_at")),
            filled_at=_parse_ts(o.get("filled_at")),
        )

    def sessions(self, through: date, count: int = 5) -> list[date]:
        """One trailing-sessions implementation in this codebase -- see
        agent/broker/simulator.py's identical delegation (Unit 4). Alpaca
        does have its own /v2/calendar endpoint; deliberately not used
        here, for the same reason SimulatorBroker no longer derives this
        itself: there is meant to be exactly one holiday-aware
        implementation, and market_calendar.trailing_sessions is it."""
        return market_calendar.trailing_sessions(through, count)

    def supported_matrix(self) -> dict[str, list[str]]:
        """Known from Alpaca's published API surface for US equities --
        NOT empirically probed against a live account. §13 asks for this
        to be probed; that requires a real (paper or live) account and has
        not been done as part of this unit. Treat this as a documented
        starting point for that probe, not its result."""
        return {
            "order_type": ["MARKET", "LIMIT", "STOP", "STOP_LIMIT", "TRAILING_STOP"],
            "time_in_force": ["DAY", "GTC", "OPG", "CLS", "IOC", "FOK"],
            "session": ["REGULAR"],
            "fractional": ["MARKET", "LIMIT"],
        }

    # -- write ----------------------------------------------------------------
    def _submit_impl(self, staged: StagedOrder) -> BrokerOrder:
        self._verify_staged_or_raise(staged, where="_submit_impl")

        # Idempotent on client_order_id: check first, mirroring
        # SimulatorBroker's in-memory short-circuit exactly (module
        # docstring's IDEMPOTENCY section) -- avoids the duplicate POST
        # entirely rather than relying only on Alpaca's error response.
        existing = self.get_by_client_id(staged.client_order_id)
        if existing is not None:
            return existing

        side = "buy" if staged.side.upper() == "BUY" else "sell"
        body: dict = {
            "symbol": staged.symbol,
            "qty": str(staged.authorized_qty),
            "side": side,
            "type": staged.order_type.lower(),
            "time_in_force": staged.time_in_force.lower(),
            "client_order_id": staged.client_order_id,
        }
        if staged.limit_price is not None:
            body["limit_price"] = str(staged.limit_price)

        try:
            status, data = self._request("POST", "/v2/orders", json_body=body, retryable=False)
        except TransportError as exc:
            raise AmbiguousOrderState(
                f"submit for client_order_id={staged.client_order_id!r} failed "
                f"with a transport error ({exc}); the order's fate at Alpaca "
                "is UNKNOWN. Resolve via get_by_client_id before doing "
                "anything else -- never resubmit."
            ) from None

        if status == 422:
            # Duplicate client_order_id (a race past the check above), or
            # some other rejection Alpaca expresses as 422 -- distinguished
            # by re-querying, not by parsing the error message text.
            existing = self.get_by_client_id(staged.client_order_id)
            if existing is not None:
                return existing
            raise AlpacaError(
                f"submit for client_order_id={staged.client_order_id!r} was "
                f"rejected (422) and no matching order exists: {data}"
            )
        if status >= 400:
            raise AlpacaError(
                f"submit for client_order_id={staged.client_order_id!r} "
                f"failed: HTTP {status}: {data}"
            )
        return self._to_broker_order(data)

    def _cancel_impl(self, staged: StagedOrder) -> BrokerOrder | None:
        self._verify_staged_or_raise(staged, where="_cancel_impl")
        client_order_id = staged.client_order_id

        existing = self.get_by_client_id(client_order_id)
        if existing is None:
            return None
        if existing.status not in ("new", "partially_filled"):
            return existing  # already resolved; nothing to cancel

        try:
            status, data = self._request(
                "DELETE", f"/v2/orders/{existing.broker_order_id}", retryable=False)
        except TransportError as exc:
            raise AmbiguousOrderState(
                f"cancel for client_order_id={client_order_id!r} failed with "
                f"a transport error ({exc}); whether the cancel reached "
                "Alpaca is UNKNOWN. Resolve via get_by_client_id before "
                "doing anything else -- never retry blindly."
            ) from None
        if status not in (200, 204):
            raise AlpacaError(
                f"cancel for client_order_id={client_order_id!r} failed: "
                f"HTTP {status}: {data}"
            )
        refreshed = self.get_by_client_id(client_order_id)
        return refreshed if refreshed is not None else existing
