"""Alpaca MARKET DATA API client (§2, §11 Day 4 collectors unit, Commit 1).

A SEPARATE PRODUCT FROM THE TRADING API (`agent/broker/alpaca.py`). Same
company, same credential pair (`APCA-API-KEY-ID`/`APCA-API-SECRET-KEY`), but
a different base URL (`data.alpaca.markets`, not `paper-api.alpaca.markets`)
and a different subscription model -- confirmed directly against Alpaca's own
docs (docs.alpaca.markets/us/docs/about-market-data-api, fetched 2026-07-31):
the Basic plan (the default for every paper AND live trading account, at zero
cost) gives real-time access to exactly one feed, IEX ("This is the only feed
that can be used without a subscription" -- docs.alpaca.markets/us/docs/
historical-stock-data-1). Full-tape SIP coverage requires the paid
"Algo Trader Plus" tier ($99/month, Trading API pricing table on the About
page). This client is built against the Basic/free tier this pilot actually
has -- see FEED, EXPLICIT NEVER DEFAULTED below.

ENDPOINT: `GET https://data.alpaca.markets/v2/stocks/bars` (the multi-symbol
historical bars endpoint, docs.alpaca.markets/reference/stockbars). One
endpoint serves both this module's uses (see agent/market_data_collector.py):
daily bars (`timeframe=1Day`) for `atr_20` and today's-so-far
`ret_since_open`/`volume_so_far`, and minute bars (`timeframe=1Min`) for the
same-time-of-day historical volume baseline `median_volume_same_time` needs.
Fetched via ONE method (`bars`), parameterized by `timeframe` -- not two
near-duplicate implementations.

FEED, EXPLICIT, NEVER DEFAULTED (the load-bearing finding of this commit).
Alpaca's own bars endpoint reference documents `feed` as defaulting to `sip`
when a caller omits it, AND documents exactly what a Basic-plan account gets
in that case -- not an error, a silent truncation: "end ... Default: the
current time if the user has a real-time access for the feed, otherwise 15
minutes before the current time" (same reference page, fetched 2026-07-31).
A Basic-plan caller that forgot to pass `feed=iex` would not see an auth
error; it would silently receive bars up to 15 minutes stale, feed=sip, with
every other parameter behaving exactly as requested. This is precisely the
kind of silent degradation Appendix E's fail-safe discipline exists to
prevent, so this client makes `feed` a required constructor argument with no
class-level default of its own (the caller -- `agent.config.Config.
market_data_feed`, itself defaulted to `"iex"`, §9.1 same-commit rule -- is
the one place a default lives) and includes it explicitly on every single
request; there is no code path in this module that omits `feed` and falls
through to Alpaca's own default.

WHAT "iex" ACTUALLY MEANS FOR THIS PILOT'S SCREEN -- NOT A CLOCK DELAY, A
COVERAGE GAP. IEX's own real-time feed carries no Alpaca-imposed time delay
(unlike the SIP-on-Basic case above) -- but IEX is one single exchange
representing approximately 2.5% of consolidated US equity volume (same
reference page). Every value this collector produces from `feed=iex`
(`ret_since_open`, `volume_so_far`, `atr_20`, `median_volume_same_time`) is
therefore computed from a thin, non-representative slice of a symbol's real
trading activity, not a time-lagged view of the whole tape. This matters
specifically for the two VOLUME-based terms in §3.2's score: `volume_so_far`
and `median_volume_same_time` will both be computed on the same ~2.5%
sub-sample, so their RATIO (`term2_volume` in `agent.materiality.
compute_score`) is less distorted by this than either raw number would be in
isolation (a systematic under-count in the numerator is echoed in the
denominator) -- but the sample is still thin enough that a single large IEX
print can swing `volume_so_far` by a proportionally large amount in a way
100%-of-tape SIP data would smooth out. This is a real, load-bearing
limitation of running this screen on Alpaca's free tier, not a footnote;
Commit 1's own delivery report states it plainly.

RATE LIMITS. Basic plan: 200 historical API calls per minute (same About
Market Data API page). This client makes exactly one HTTP request per
`bars()` call (plus one more per continuation page, via `next_page_token`,
for a response that hits `limit`) -- no client-side throttling is
implemented here because a T1 collection cycle (`data_collection_interval_
seconds`, default 60s, agent/config.py) against a small, fixed symbol
universe (see agent/universe.py, Commit 4) stays far below 200 calls/minute
by construction; this is a documented assumption, not an enforced limit, and
a future universe large enough to approach it would need its own throttle,
not a change to this module.

TRANSPORT -- SAME INJECTED PATTERN AS `AlpacaPaperAdapter`
(agent/broker/alpaca.py). `Transport` (agent/broker/transport.py) is shared
between both modules; this client never imports `urllib` itself. Reads only
-- there is no write method anywhere in this class, matching
`scripts/alpaca_probe.py`'s own read-only-by-construction discipline (search
this file for "POST"/"PUT"/"PATCH"/"DELETE": they do not appear).

RETRY POLICY: every call this client makes is a read (historical bars have no
side effects), so every call is retryable, bounded by `http_max_retries` --
unlike `AlpacaPaperAdapter`, there is no write path here that would need the
opposite (never-retry) discipline.
"""
from __future__ import annotations

from datetime import datetime, timezone

from ..accounts import BrokerCredentials
from ..secrets_provider import SecretsProvider
from .transport import Transport, TransportError, UrllibTransport

_EXPECTED_SECRETS_MODE = "PAPER"

# Alpaca's own documented feed enum (docs.alpaca.markets/reference/
# stockbars). "iex" is the only one usable without a paid subscription --
# see module docstring's FEED section for why this client never lets a
# caller omit it and fall through to Alpaca's own "sip" default.
VALID_FEEDS = frozenset({"iex", "sip", "boats", "otc"})


class AlpacaMarketDataError(Exception):
    pass


def _format_ts(dt: datetime) -> str:
    """RFC-3339, matching what Alpaca's own bars endpoint documents for
    `start`/`end` (docs.alpaca.markets/reference/stockbars)."""
    if dt.tzinfo is None:
        raise AlpacaMarketDataError("datetime arguments must be timezone-aware")
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class AlpacaMarketDataClient:
    """Alpaca's Market Data API (§11 Day 4). See module docstring for why
    this is a separate class from `AlpacaPaperAdapter` (a different base
    URL and subscription model, sharing only the credential pair and
    `Transport` abstraction)."""

    BASE_URL = "https://data.alpaca.markets"

    def __init__(self, *, credentials: BrokerCredentials | None,
                secrets_provider: SecretsProvider, feed: str,
                transport: Transport | None = None,
                http_timeout_seconds: float = 10.0, http_max_retries: int = 2):
        if credentials is None:
            raise AlpacaMarketDataError("credentials are required")
        if feed not in VALID_FEEDS:
            raise AlpacaMarketDataError(
                f"feed must be one of {sorted(VALID_FEEDS)}, got {feed!r}"
            )
        if secrets_provider.mode != _EXPECTED_SECRETS_MODE:
            raise AlpacaMarketDataError(
                f"AlpacaMarketDataClient is bound to mode={_EXPECTED_SECRETS_MODE!r}, "
                f"but was given a secrets_provider bound to mode={secrets_provider.mode!r}."
            )
        self._credentials = credentials
        self._secrets = secrets_provider
        self._feed = feed
        self._transport = transport or UrllibTransport()
        self._timeout = http_timeout_seconds
        self._max_retries = http_max_retries

    def _headers(self) -> dict[str, str]:
        # Resolved fresh on every call, never cached -- same discipline as
        # AlpacaPaperAdapter._headers (agent/broker/alpaca.py).
        return {
            "APCA-API-KEY-ID": self._credentials.key_id,
            "APCA-API-SECRET-KEY": self._secrets.resolve(self._credentials.secret_ref),
        }

    def _get(self, path: str, params: dict) -> dict:
        url = f"{self.BASE_URL}{path}"
        attempts = self._max_retries + 1   # every call here is a read -- always retryable
        last_exc: TransportError | None = None
        for _ in range(attempts):
            try:
                status, body = self._transport.request(
                    "GET", url, headers=self._headers(), params=params, timeout=self._timeout)
            except TransportError as exc:
                last_exc = exc
                continue
            if status >= 400:
                raise AlpacaMarketDataError(f"GET {path} failed: HTTP {status}: {body}")
            return body
        assert last_exc is not None
        raise last_exc

    def bars(self, symbols: list[str], *, timeframe: str,
            start: datetime | None = None, end: datetime | None = None,
            limit: int = 10000) -> dict[str, list[dict]]:
        """Multi-symbol historical bars, paginated via `next_page_token`
        until Alpaca reports none left. `feed` is always this client's own
        bound value (module docstring's FEED section) -- never omitted, so
        Alpaca's own "sip if you don't say otherwise" default is never
        reachable from this method. Returns `{symbol: [bar, ...]}`, each bar
        a raw dict as Alpaca reports it (`t`, `o`, `h`, `l`, `c`, `v`, `n`,
        `vw`) -- oldest first (Alpaca's own default sort), across however
        many pages it took.

        `limit=10000` (Alpaca's own documented maximum per page, not this
        pilot's per-symbol expectation) -- this pilot's actual per-call bar
        counts (25 daily bars, ~20 sessions x ~390 1-minute bars for a small
        universe) are well under Alpaca's page cap in the common case, so
        pagination is expected to be rare, not absent -- it is still fully
        implemented, not assumed away."""
        if not symbols:
            raise AlpacaMarketDataError("symbols must be non-empty")
        out: dict[str, list[dict]] = {s: [] for s in symbols}
        page_token: str | None = None
        while True:
            params: dict = {
                "symbols": ",".join(symbols), "timeframe": timeframe,
                "feed": self._feed, "limit": str(limit), "sort": "asc",
            }
            if start is not None:
                params["start"] = _format_ts(start)
            if end is not None:
                params["end"] = _format_ts(end)
            if page_token is not None:
                params["page_token"] = page_token
            body = self._get("/v2/stocks/bars", params)
            page_bars = body.get("bars") or {}
            for symbol, bar_list in page_bars.items():
                out.setdefault(symbol, []).extend(bar_list)
            page_token = body.get("next_page_token")
            if not page_token:
                break
        return out

    def daily_bars(self, symbols: list[str], *, end: datetime,
                   limit: int = 25) -> dict[str, list[dict]]:
        """`timeframe=1Day` bars ending at `end` (normally "now" -- the
        current, still-forming session's own bar is the last entry when the
        market is open, and is exactly today's-so-far OHLCV `agent.
        market_data_collector` needs for `ret_since_open`/`volume_so_far`;
        see that module for how the two are told apart from the prior
        `limit - 1` COMPLETE sessions `atr_20` needs)."""
        return self.bars(symbols, timeframe="1Day", end=end, limit=limit)

    def minute_bars(self, symbols: list[str], *, start: datetime,
                   end: datetime) -> dict[str, list[dict]]:
        """`timeframe=1Min` bars over `[start, end]` -- the granularity
        `median_volume_same_time` needs to compare "volume so far today" (at
        `end`'s own minute-of-day) against the same minute-of-day on each of
        several prior sessions."""
        return self.bars(symbols, timeframe="1Min", start=start, end=end)
