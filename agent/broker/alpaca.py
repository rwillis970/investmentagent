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

    settled_cash = Decimal(data["cash"])   -- the closest available figure
                                              (parsed exactly, from the
                                              decimal string Alpaca reports
                                              -- see _dec and the Decimal
                                              migration note below)
    unsettled_cash = Decimal("0")           -- ALWAYS, because there is
                                              nothing to compute it from

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

DECIMAL, NOT FLOAT (found running the loop against the real paper account,
2026-07-28: a fractional-share fill produced a local settled-cash figure
that disagreed with the broker's own at the fifteenth decimal place --
representational float noise, not a real discrepancy, tripping the exact-
equality reconciliation check in agent/reconciliation.py). Every money and
quantity field this adapter parses -- `equity`, `cash`, `settled_cash`,
`buying_power`, `multiplier`, position `qty`/`avg_price`/`market_value`,
order `qty`/`limit_price`/`filled_qty`/`avg_fill_price`, and execution
`qty`/`price`/`cum_qty` -- is now a `decimal.Decimal`, parsed via
`agent.money.to_decimal` (imported here as `_dec`) directly from the JSON
string Alpaca reports, never via `float(...)`. Alpaca's own API already
reports these as decimal strings, so this is a lossless re-parse, not an
approximation layered on top of one: the exact digits Alpaca sent are the
exact digits this adapter now holds. `to_decimal` never calls
`Decimal(a_float)` -- that would capture the float's own binary imprecision
rather than curing it -- routing any float input through `str()` first
instead (see agent/money.py, the one place this rule lives). See
agent/ledger.py and agent/reconciliation.py for where this exactness is
actually load-bearing (an exact-equality comparison against a local
ledger's own Decimal arithmetic).

EMPIRICALLY CONFIRMED (§13 probe, scripts/alpaca_probe.py, captured
2026-07-27T18:00:18Z -- see scripts/fixtures/): a real paper `/v2/account`
response for a brand-new $500 cash account was dumped verbatim -- 36 top-
level fields, none named anything like "settled cash" or "cash_withdrawable".
This isn't just re-confirming the alpaca-py model read above; it's the raw
wire response. So the answer to "can settled be distinguished from unsettled
in a cash account" is a confirmed NO, not an inferred one: there is no field,
anywhere in `/v2/account`, that could be remapped onto a settled/unsettled
split. `AccountSnapshot` is NOT changed as a result -- there is nothing to
change it TO. What this DOES leave open, because the captured account had
never placed an order: whether Alpaca's `cash` figure itself moves the
instant a sale fills (i.e., whether `cash` ever transiently includes
unsettled proceeds at all) is still unobserved. That would need a second
capture bracketing a real sell + T+1 cycle -- a different, larger unit than
this one, since it requires placing and holding a real order, not just
reading account state.

STATUS MAPPING -- see `STATUS_MAP` below for the full table and which of
Alpaca's seventeen `OrderStatus` values do not map cleanly onto this
codebase's five-state vocabulary (new/partially_filled/filled/canceled/
rejected, per `agent.broker.base.BrokerOrder`).

STATUS MAPPING REMAINS UNCONFIRMED AGAINST A REAL ACCOUNT (§13 probe,
2026-07-27 -- see scripts/fixtures/orders.json). The captured account had
never placed an order, so `orders.json` is `[]`: zero of the seventeen
statuses were observed, and none of the five judgment-call mappings below
were exercised. `STATUS_MAP` is UNCHANGED -- there is no evidence either
way. This is a structural limitation of a read-only probe against a fresh
account, not an oversight: confirming real status vocabulary requires an
account with actual order history, which means either placing real orders
(out of scope for a read-only unit) or capturing again once the paper
account has some.

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
from decimal import Decimal

from ..accounts import BrokerCredentials
from ..money import to_decimal as _dec
from ..pipeline import StagedOrder
from ..policy import TradeCapabilityPolicy
from .. import market_calendar
from ..secrets_provider import SecretsProvider
from .base import (AccountActivity, AccountSnapshot, AdapterError, BrokerAdapter,
                   BrokerOrder, Execution, Position)
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
            equity=_dec(data["equity"]),
            cash=_dec(data["cash"]),
            # APPROXIMATE, not exact -- see module docstring's CASH FIELD
            # MAPPING section. Alpaca's /v2/account has no settled/
            # unsettled split.
            settled_cash=_dec(data["cash"]),
            unsettled_cash=Decimal("0"),
            buying_power=_dec(data["buying_power"]),
            multiplier=_dec(data["multiplier"]),
            # FINDING (§13 probe, 2026-07-27 -- scripts/fixtures/account.json):
            # a real cash account omits BOTH of these keys entirely -- not
            # `false`/`0`, absent. alpaca-py's own TradeAccount models both as
            # Optional[None] (fetched from github.com/alpacahq/alpaca-py),
            # confirming this is Alpaca's real behaviour, not a capture
            # artifact. Every OTHER boolean on this account (trading_blocked,
            # transfers_blocked, shorting_enabled, ...) IS present even when
            # false, which is what makes this pair's absence notable rather
            # than "Alpaca omits falsy fields" in general.
            #
            # `.get(key)` with NO default -- an absent key maps to Python
            # `None`, which `AccountSnapshot.pattern_day_trader`/
            # `day_trade_count` now model explicitly as UNKNOWN, never
            # silently coerced to `False`/`0`. Appendix E's
            # fail-safe-to-NO-TRADE forbids inventing a concrete value for an
            # absent safety-relevant field; see
            # `agent.daytrade.DayTradeGuard.reconcile` for how the unknown
            # count is actually handled at the point it matters.
            pattern_day_trader=(bool(data["pattern_day_trader"])
                               if data.get("pattern_day_trader") is not None else None),
            day_trade_count=(int(data["daytrade_count"])
                            if data.get("daytrade_count") is not None else None),
            fetched_at=datetime.now(timezone.utc),
        )

    def positions(self) -> list[Position]:
        _, data = self._request("GET", "/v2/positions", retryable=True)
        out = []
        for p in data:
            qty = _dec(p["qty"])
            if p.get("side") == "short":
                # Shorting is DISABLED at the capability layer (Appendix E)
                # for this pilot regardless -- if one somehow existed, it
                # must be reported faithfully (broker state is the source
                # of truth), not coerced positive, so reconciliation can
                # flag it as the anomaly it would be.
                qty = -qty
            avg_price = _dec(p["avg_entry_price"])
            mv_raw = p.get("market_value")
            market_value = _dec(mv_raw) if mv_raw not in (None, "") else qty * avg_price
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
            qty = _dec(filled)
        else:
            qty = _dec(qty_raw)

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
            limit_price=_dec(limit_price) if limit_price not in (None, "") else None,
            status=status,
            filled_qty=_dec(o.get("filled_qty") or "0"),
            avg_fill_price=_dec(avg_fill_price) if avg_fill_price not in (None, "") else None,
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

    def fills(self) -> list[Execution]:
        """Alpaca's Account Activities `FILL` type -- confirmed, not
        assumed, via `alpaca-py`'s own `TradeActivity` model
        (github.com/alpacahq/alpaca-py, alpaca/trading/models.py, fetched
        2026-07-27) -- is the one place this adapter reads a PER-EXECUTION
        record rather than a per-order cumulative/averaged one (see
        `Execution`'s own docstring). `id` is a stable, per-execution id
        ("timestamp::uuid" per `BaseActivity`'s own docstring); `price` is
        the per-share price THIS execution occurred at (NOT a running
        average -- that's `/v2/orders`' `filled_avg_price`, deliberately
        not used here); `qty` is this increment only; `cum_qty` is the
        cumulative total as of this increment.

        WRONG PREMISE, FOUND WHILE IMPLEMENTING (not assumed away): a
        `TradeActivity` does NOT carry `client_order_id` -- only Alpaca's
        own broker-side `order_id` (a UUID). `Execution.client_order_id`
        is required by this adapter's contract (it is how `sync_fills`
        looks up the `OrderRecord` this order was staged with), so each
        DISTINCT `order_id` seen in a page of activities is resolved to
        its `client_order_id` via one `GET /v2/orders/{order_id}` call,
        memoized within this single `fills()` call so an order with many
        partial-fill activities costs one extra request, not N.

        PAGINATION: pages forward with `direction=asc` (oldest first) and
        Alpaca's own `page_token` (the last-seen activity's `id`), in
        batches of `page_size`, until a page comes back shorter than
        `page_size` -- the documented signal that there is nothing left to
        fetch. This is the actual, complete implementation: every
        activity, oldest to newest, across as many pages as exist. It does
        not special-case a tie in `transaction_time` at a page boundary
        beyond what `page_token` itself provides."""
        activities: list[dict] = []
        page_token: str | None = None
        page_size = 100
        while True:
            params: dict = {"direction": "asc", "page_size": str(page_size)}
            if page_token is not None:
                params["page_token"] = page_token
            _, data = self._request(
                "GET", "/v2/account/activities/FILL", params=params, retryable=True)
            if not data:
                break
            activities.extend(data)
            if len(data) < page_size:
                break
            page_token = data[-1]["id"]

        client_order_ids: dict[str, str] = {}
        executions = []
        for a in activities:
            order_id = a["order_id"]
            client_order_id = client_order_ids.get(order_id)
            if client_order_id is None:
                client_order_id = self._client_order_id_for(order_id)
                client_order_ids[order_id] = client_order_id
            executions.append(self._to_execution(a, client_order_id))
        return executions

    def non_fill_activities(self) -> list[AccountActivity]:
        """Every non-FILL Account Activity this account has -- fees,
        journals, dividends, interest, and everything else the endpoint
        returns. Found real, not hypothetical (cash-event quarantine unit,
        2026-07-30): a Consolidated Audit Trail (CAT) regulatory fee posted
        overnight against a real Alpaca paper account
        (`scripts/fixtures/activities_since.json`), charged per trade, so
        this is the normal case, not an edge case.

        HITS THE GENERAL ENDPOINT, NO `activity_types` FILTER -- same
        no-allowlist choice `scripts/alpaca_probe.py`'s own
        `_fetch_all_activities_since` already makes, for the same reason:
        an allowlist of the ~35 documented activity types is exactly the
        kind of guess Appendix E's fail-safe bias argues against (a type
        added to Alpaca's API after this list was written would silently
        never be reported). `fills()` already covers `FILL` via the
        type-specific `/v2/account/activities/FILL` endpoint; this method
        excludes `FILL` rows locally rather than asking the general
        endpoint to do it, since Alpaca's own API has no "exclude type"
        parameter, only an inclusion list.

        PAGINATION: identical shape to `fills()` -- `direction=asc`,
        `page_token` from the last-seen activity's own `id`, stops on a
        page shorter than `page_size`. One paginated read, not two
        separate polling mechanisms."""
        activities: list[dict] = []
        page_token: str | None = None
        page_size = 100
        while True:
            params: dict = {"direction": "asc", "page_size": str(page_size)}
            if page_token is not None:
                params["page_token"] = page_token
            _, data = self._request(
                "GET", "/v2/account/activities", params=params, retryable=True)
            if not data:
                break
            activities.extend(data)
            if len(data) < page_size:
                break
            page_token = data[-1]["id"]

        # FILTER FIRST, AS ITS OWN STEP -- a FILL row is excluded BY
        # DEFINITION of this method and must never reach
        # _to_account_activity at all (real defect, 2026-07-31: the first
        # unattended launchd run crashed with KeyError: 'created_at' --
        # see agent/broker/base.py's own docstring's created_at section
        # for why a FILL row does not carry one). A single list
        # comprehension with the `if` on the same expression already
        # filters before mapping (Python's own comprehension semantics),
        # but writing it as two explicit steps makes that ordering an
        # auditable fact about THIS code, not a property a future edit
        # could accidentally invert (e.g. mapping first "for convenience"
        # and filtering the mapped objects afterward).
        non_fill = [a for a in activities if a.get("activity_type") != "FILL"]
        return [self._to_account_activity(a) for a in non_fill]

    def _to_account_activity(self, a: dict) -> AccountActivity:
        return AccountActivity(
            activity_id=a["id"], account_id=self.account_id,
            activity_type=a["activity_type"],
            activity_sub_type=a.get("activity_sub_type"),
            net_amount=_dec(a["net_amount"]), date=date.fromisoformat(a["date"]),
            # created_at: `.get(...)`, NOT `a["created_at"]` (real defect,
            # 2026-07-31) -- confirmed present for JNLC and FEE, the only
            # two of the ~35 documented Account Activities types this
            # account has ever produced (scripts/fixtures/
            # activities_since.json); NOT confirmed for the other ~33,
            # which this system has never observed. Assuming universal
            # presence from two samples is exactly the kind of guess
            # Appendix E's fail-safe bias argues against. `_parse_ts`
            # already returns `None` for a falsy/absent input -- see
            # agent/broker/base.py's own docstring for what a `None` here
            # means downstream (the pre-baseline admission guard refuses
            # outright rather than guessing).
            created_at=_parse_ts(a.get("created_at")),
            symbol=a.get("symbol"), description=a.get("description", ""),
        )

    def _client_order_id_for(self, broker_order_id: str) -> str:
        """Resolve an Alpaca broker-side order id to the `client_order_id`
        it was submitted with -- `TradeActivity` reports only the former
        (see `fills()`'s docstring)."""
        _, data = self._request("GET", f"/v2/orders/{broker_order_id}", retryable=True)
        return data["client_order_id"]

    def _to_execution(self, a: dict, client_order_id: str) -> Execution:
        return Execution(
            execution_id=a["id"],
            account_id=self.account_id,
            client_order_id=client_order_id,
            symbol=a["symbol"],
            side=a["side"].upper(),
            qty=_dec(a["qty"]),
            price=_dec(a["price"]),
            cum_qty=_dec(a["cum_qty"]),
            filled_at=_parse_ts(a["transaction_time"]),
        )

    def supported_matrix(self) -> dict[str, list[str]]:
        """§13 empirical probe, capture dates 2026-07-27 (account.json) and
        the follow-up capture that added configurations.json and assets.json
        (SPY/QQQ/AAPL) -- see scripts/fixtures/. Per-key status below; this
        replaces the earlier "neither confirmed nor contradicted" note now
        that the two endpoints it named have actually been probed.

        order_type / time_in_force -- STILL AN UNVERIFIED GUESS, from
        Alpaca's published API surface, not this account. None of
        `/v2/account`, `/v2/account/configurations` or `/v2/assets/{symbol}`
        expose a supported-order-type or supported-time-in-force list --
        these are fixed API features, not account- or asset-scoped data, so
        there is no endpoint left to probe for them; a real answer would
        require attempting real orders and observing acceptance/rejection,
        which is a write action, out of scope for a read-only probe.

        session -- CONTRADICTED, and now corrected. The old guess was
        `["REGULAR"]` only. `configurations.json` reports
        `disable_overnight_trading: false` (overnight trading is NOT
        disabled for this account), and all three probed assets (SPY, QQQ,
        AAPL) carry BOTH `overnight_tradable` and `fractional_eh_enabled` in
        their `attributes` list -- i.e. this account, on these symbols, can
        trade OVERNIGHT and can trade fractional quantities during EXTENDED
        hours. Updated to `["REGULAR", "EXTENDED", "OVERNIGHT"]`, matching
        the exact vocabulary `agent.policy.initial_policy`'s own `session`
        capability dict already uses -- that policy had already anticipated
        this three-way distinction and disables EXTENDED/OVERNIGHT by
        default (Appendix E: "Regular market hours"). This matrix and that
        policy answer DIFFERENT questions -- this is what the broker can do;
        the policy is what this pilot currently allows -- and this update is
        NOT a proposal to enable EXTENDED/OVERNIGHT trading. Confirmed for
        three large, liquid, easy-to-borrow symbols only; not confirmed
        universe-wide.

        fractional -- PARTIALLY CONFIRMED. `configurations.json` reports
        `fractional_trading: true` account-wide, and all three probed assets
        report `fractionable: true` -- fractional trading being enabled and
        available for these symbols is now confirmed, not guessed. NOT
        confirmed: which specific order types accept a fractional quantity
        -- neither endpoint breaks that down by order type, so this list
        (`["MARKET", "LIMIT"]`) remains an unverified guess, unchanged.

        Incidental finding, not modeled by any key here: `account.json`
        reports `shorting_enabled: false` account-wide, while
        `configurations.json` reports `no_shorting: false` (not explicitly
        long-only) and all three assets report `shortable: true` --
        genuinely in tension, not resolved here, and moot in practice since
        shorting is independently DISABLED at the capability layer
        regardless (Appendix E)."""
        return {
            "order_type": ["MARKET", "LIMIT", "STOP", "STOP_LIMIT", "TRAILING_STOP"],
            "time_in_force": ["DAY", "GTC", "OPG", "CLS", "IOC", "FOK"],
            "session": ["REGULAR", "EXTENDED", "OVERNIGHT"],
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
