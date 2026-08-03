"""SEC EDGAR client (§2, §11 Day 4 collectors unit, Commit 2).

TWO OFFICIALLY DOCUMENTED, STABLE ENDPOINTS -- NOT THE FULL-TEXT-SEARCH
BACKEND. SEC's own developer-resources page (sec.gov/search-filings/edgar-
application-programming-interfaces, fetched 2026-07-31) documents exactly
two REST data APIs under `data.sec.gov`: filing history by company
(`submissions`) and XBRL facts. The EDGAR Full-Text-Search UI's own backend
(`efts.sec.gov/LATEST/search-index`) is real, working, and was directly
observed during this unit returning clean, structured item-code data -- but
it is NOT among the endpoints that page documents as a stable, supported
API, so this client is built on the documented one instead, per this
codebase's general preference for confirmed-and-documented over merely-
observed-to-work (the same standard `agent/broker/alpaca.py` already holds
itself to, e.g. "confirmed against alpaca-py's own model", "STILL AN
UNVERIFIED GUESS" call-outs for anything it could not pin down that way).

1. `https://www.sec.gov/files/company_tickers.json` -- the ticker/CIK/name
   mapping (same developer-resources FAQ page: "Do you have a file that
   maps company ticker / CIK / company name? ... company_tickers.json").
   SEE `agent.edgar_collector.TickerCikCache` for why and how often this is
   refreshed, not fetched once and trusted forever.
2. `https://data.sec.gov/submissions/CIK##########.json` -- one company's
   filing history, "a compact columnar data array" (SEC's own EDGAR-APIs
   page): each field (`form`, `filingDate`, `reportDate`,
   `acceptanceDateTime`, `items`, `accessionNumber`, `primaryDocument`, ...)
   is a PARALLEL ARRAY under `filings.recent`, not one object per filing --
   `_parse_recent_filings` below un-columns it back into one dict per
   filing. If a filer has more than "at least one year's... or to 1,000...
   of the most recent filings" (SEC's own wording), `filings.files` names
   additional JSON files at `https://data.sec.gov/submissions/{name}`,
   fetched here too (`_older_filings_urls`) -- this is fully implemented,
   not assumed unnecessary for a small pilot universe.

WHAT THIS CLIENT DOES NOT CONFIRM FIRSTHAND: THE EXACT `items` WIRE SHAPE IN
`submissions.json`. Multiple independent secondary sources (fetched during
this unit's research) describe `items` in the `filings.recent` columnar
block as a single, comma-joined STRING per filing (e.g. `"2.02,9.01"`),
consistent with every other field in that block being a flat scalar array,
not a nested one -- and this is corroborated structurally by the same
information appearing as a genuine JSON ARRAY (`["2.02","9.01"]`) in the
DIFFERENT, unofficial full-text-search endpoint observed during this unit's
research (not itself used here -- see above). This client's own attempt to
fetch a real `submissions.json` and inspect the `items` array directly was
cut short by this development environment's own response-size limit before
reaching that field (the file is tens of thousands of characters for an
active filer). `_parse_item_codes` below is therefore DELIBERATELY
DEFENSIVE: it accepts either a comma-joined string or a JSON list and
normalises both to a tuple of codes, rather than assuming one shape and
raising confusingly on the other. Flagged here, not silently assumed
correct -- verifying the real wire shape against a live account (the same
kind of empirical confirmation `scripts/alpaca_probe.py` exists to do for
Alpaca) is a natural follow-up, not attempted in this unit.

RATE LIMIT: 10 REQUESTS/SECOND, ENFORCED IN CODE (confirmed directly against
sec.gov/about/webmaster-frequently-asked-questions, fetched 2026-07-31: "our
current maximum access rate is 10 requests per second... regardless of the
number of machines used"). `_RateLimiter` below is a real, sleeping throttle
every `_get` call goes through -- not a comment asking a caller to be
polite. See agent/config.py's own comment for why the default interval
(0.15s, ~6.7 req/s) is deliberately below the documented ceiling.

USER-AGENT, REQUIRED, NEVER A CANNED DEFAULT (same FAQ page: "Please
declare your user agent in request headers" naming the requester and a
contact email). `EdgarClient` takes `user_agent` as a required constructor
argument with no class-level fallback; `agent.config.Config.
edgar_user_agent` (this codebase's one place a default could live) has an
EMPTY default specifically so a config that never set it fails validation
loudly rather than this client silently sending some placeholder identity
that isn't Ray's own.

TRANSPORT: same injected `Transport` abstraction (agent/broker/
transport.py) `AlpacaPaperAdapter`/`AlpacaMarketDataClient` already use --
no test in this codebase's suite for this module ever makes a network call.

READ-ONLY. Every method here issues a GET; there is no write path (EDGAR
itself is a read-only public disclosure system from this pilot's
perspective -- nothing in this codebase files anything with the SEC).
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from datetime import date, datetime

from .broker.alpaca import _parse_ts as parse_edgar_timestamp
from .broker.transport import Transport, TransportError, UrllibTransport

TICKER_CIK_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik10}.json"
SUBMISSIONS_BASE = "https://data.sec.gov/submissions/"

# Filing DOCUMENT BODY fetch (T4 prerequisite unit, 2026-07-31). CONFIRMED
# directly against SEC's own "Accessing EDGAR Data" page (sec.gov/search-
# filings/edgar-search-assistance/accessing-edgar-data, fetched 2026-07-31):
# "Post-EDGAR 7.0 filings (after May 26, 2000) are also accessible via an
# alternative symbolic path, incorporating an intermediate accession-number
# directory without dashes" -- e.g. /Archives/edgar/data/1122304/
# 000119312515118890/0001193125-15-118890.txt. That page's own examples use
# the CIK WITHOUT leading zeros (e.g. "51143", "1122304") -- DIFFERENT from
# `SUBMISSIONS_URL` above, which zero-pads to 10 digits; this is a distinct
# endpoint with its own, separately-confirmed convention, not an inconsistency
# in this module.
ARCHIVES_BASE = "https://www.sec.gov/Archives/edgar/data"

# §3.2's own allowlist (agent/materiality.py) -- reused, not duplicated: the
# set of forms this collector bothers fetching is exactly the set
# filing_weight() ever assigns nonzero weight to. An amendment like
# "10-K/A" is deliberately NOT included here, matching filing_weight's own
# exact-string match today (form.upper() == "10-K"); an amendment neither
# scores under the current allowlist nor is collected here -- consistent,
# not a new gap this module introduces.
from .materiality import WEIGHTED_FORMS  # noqa: E402  (after TICKER_CIK_URL for readability)

ALLOWED_FORMS = frozenset({"8-K"}) | WEIGHTED_FORMS


class EdgarError(Exception):
    pass


class _RateLimiter:
    """A real, sleeping throttle -- not a comment. Enforces at least
    `min_interval_seconds` between the START of one request and the START
    of the next, tracked via a monotonic clock (never wall-clock time,
    which can jump backwards across an NTP correction or a laptop resuming
    from sleep). `sleep_fn`/`monotonic_fn` are injectable so tests can
    assert on throttling behaviour without a real test run ever actually
    sleeping for it."""

    def __init__(self, min_interval_seconds: float, *,
                sleep_fn=time.sleep, monotonic_fn=time.monotonic):
        if min_interval_seconds <= 0:
            raise EdgarError("min_interval_seconds must be positive")
        self._min_interval = min_interval_seconds
        self._sleep = sleep_fn
        self._monotonic = monotonic_fn
        self._last_call: float | None = None

    def throttle(self) -> None:
        now = self._monotonic()
        if self._last_call is not None:
            elapsed = now - self._last_call
            remaining = self._min_interval - elapsed
            if remaining > 0:
                self._sleep(remaining)
                now = self._monotonic()
        self._last_call = now


def _document_url(cik: str, accession_number: str, primary_document: str) -> str:
    """`https://www.sec.gov/Archives/edgar/data/{cik, no leading zeros}/
    {accession number, dashes removed}/{primary document filename}` --
    CONFIRMED directly against SEC's own page (see `ARCHIVES_BASE`'s own
    comment above for the citation). `cik` is accepted either zero-padded
    (as stored in this codebase's own `filing` metadata Facts, matching
    `SUBMISSIONS_URL`'s convention) or already bare -- `int()` normalises
    either to the unpadded form this endpoint's own examples use."""
    cik_unpadded = str(int(cik))
    accession_nodashes = accession_number.replace("-", "")
    return f"{ARCHIVES_BASE}/{cik_unpadded}/{accession_nodashes}/{primary_document}"


@dataclass(frozen=True)
class FilingDocumentFetch:
    """The result of fetching one filing's primary document body.

    `sha256` is computed over the ACTUAL STORED bytes (i.e. after any
    truncation), never a hypothetical full body this client never fully
    received -- see agent/edgar_collector.py's module docstring for why
    this is the correct identity for Commit 4's extraction cache: two
    fetches of the same document that truncate at different points are
    genuinely different partial artifacts and must not collide under the
    same cache key.

    `text` is the raw body decoded as UTF-8 with `errors="replace"` --
    EDGAR HTML filings are expected to be UTF-8/ASCII; a replacement
    character on rare bad bytes is preferred over raising and losing the
    rest of an otherwise-good document. This is about narrative-text
    fidelity, not byte-exact reproduction -- `sha256`/`byte_length` above
    are computed against the original bytes, before decoding, specifically
    so a decode's own lossiness never affects the integrity/cache-key
    signal."""
    text: str
    sha256: str
    byte_length: int
    truncated: bool


def _parse_item_codes(raw) -> tuple[str, ...]:
    """Defensive against the two shapes this field could plausibly take in
    `submissions.json` -- see module docstring's WHAT THIS CLIENT DOES NOT
    CONFIRM section for why this isn't a single, confident branch."""
    if not raw:
        return ()
    if isinstance(raw, (list, tuple)):
        return tuple(str(code) for code in raw if code)
    if isinstance(raw, str):
        return tuple(code.strip() for code in raw.split(",") if code.strip())
    raise EdgarError(f"unrecognised items field shape: {raw!r}")


def _parse_recent_filings(recent: dict) -> list[dict]:
    """Un-columns `filings.recent`'s parallel-array shape (SEC's own
    "compact columnar data array") into one dict per filing, oldest-order
    preserved as given (SEC's own ordering, not re-sorted here)."""
    forms = recent.get("form", [])
    n = len(forms)
    out = []
    for i in range(n):
        report_date_raw = (recent.get("reportDate") or [None] * n)[i]
        accepted_raw = (recent.get("acceptanceDateTime") or [None] * n)[i]
        out.append({
            "form": forms[i],
            "filing_date": recent["filingDate"][i],
            "report_date": report_date_raw or None,
            "accepted_at": accepted_raw or None,
            "accession_number": recent["accessionNumber"][i],
            "primary_document": (recent.get("primaryDocument") or [None] * n)[i],
            "item_codes": _parse_item_codes((recent.get("items") or [None] * n)[i]),
        })
    return out


class EdgarClient:
    """See module docstring for the two endpoints this wraps, the rate
    limit, and the required User-Agent."""

    def __init__(self, *, user_agent: str, transport: Transport | None = None,
                http_timeout_seconds: float = 10.0, http_max_retries: int = 2,
                min_request_interval_seconds: float = 0.15,
                sleep_fn=time.sleep, monotonic_fn=time.monotonic):
        if not user_agent or "@" not in user_agent:
            raise EdgarError(
                "user_agent must name a requester and contact email, e.g. "
                "'InvestmentAgent Pilot ray@example.com' -- EDGAR's own "
                "acceptable-use policy requires this on every request"
            )
        self._user_agent = user_agent
        self._transport = transport or UrllibTransport()
        self._timeout = http_timeout_seconds
        self._max_retries = http_max_retries
        self._limiter = _RateLimiter(min_request_interval_seconds,
                                     sleep_fn=sleep_fn, monotonic_fn=monotonic_fn)

    def _get(self, url: str, *, params: dict | None = None) -> dict:
        # Accept-Encoding: identity, NOT "gzip, deflate" (production defect,
        # 2026-08-03). `Transport`/`UrllibTransport` (agent/broker/
        # transport.py) never inspects Content-Encoding and never
        # decompresses -- `request`/`request_raw` hand back exactly the
        # bytes `resp.read()` returned. Advertising gzip support here while
        # the transport can't decode it means a real EDGAR response
        # compressed in reply (confirmed: byte 0x8b at position 1 is the
        # second byte of the 1f 8b gzip magic number) reaches `json.loads`
        # (here) or `.decode("utf-8", ...)` (`filing_document`, below) as
        # raw gzip bytes, which fails with exactly the reported
        # `'utf-8' codec can't decode byte 0x8b in position 1` error --
        # this halted a real running PAPER-mode loop during data
        # collection. `identity` tells the server not to compress the
        # response at all, so there is nothing for either call site to
        # decompress. THE NARROW FIX FOR THIS PILOT: general compression
        # support would need Content-Encoding threaded back through
        # `Transport` and `_read_capped`'s byte cap applied to the
        # decompressed body, not the wire body -- out of scope here; see
        # this unit's own delivery report.
        headers = {"User-Agent": self._user_agent, "Accept-Encoding": "identity"}
        attempts = self._max_retries + 1   # every call here is a read
        last_exc: TransportError | None = None
        for _ in range(attempts):
            self._limiter.throttle()
            try:
                status, body = self._transport.request(
                    "GET", url, headers=headers, params=params, timeout=self._timeout)
            except TransportError as exc:
                last_exc = exc
                continue
            if status >= 400:
                raise EdgarError(f"GET {url} failed: HTTP {status}: {body}")
            return body
        assert last_exc is not None
        raise last_exc

    def _get_raw(self, url: str, *, max_bytes: int | None) -> tuple[bytes, bool]:
        """Same retry/throttle/User-Agent discipline as `_get`, over
        `Transport.request_raw` instead of `request` -- a filing document
        body is not a JSON API response, so `_get` cannot serve it (see
        `agent.broker.transport.Transport.request_raw`'s own docstring).

        Accept-Encoding: identity here too, same reasoning as `_get`'s own
        comment above -- a compressed filing document would otherwise reach
        `filing_document`'s `.decode("utf-8", ...)` below as raw gzip bytes
        and fail the same way."""
        headers = {"User-Agent": self._user_agent, "Accept-Encoding": "identity"}
        attempts = self._max_retries + 1   # every call here is a read
        last_exc: TransportError | None = None
        for _ in range(attempts):
            self._limiter.throttle()
            try:
                status, body, truncated = self._transport.request_raw(
                    "GET", url, headers=headers, timeout=self._timeout, max_bytes=max_bytes)
            except TransportError as exc:
                last_exc = exc
                continue
            if status >= 400:
                raise EdgarError(f"GET {url} failed: HTTP {status}")
            return body, truncated
        assert last_exc is not None
        raise last_exc

    def filing_document(self, cik: str, accession_number: str, primary_document: str, *,
                        max_bytes: int) -> FilingDocumentFetch:
        """Fetch one filing's primary document BODY (the actual narrative
        HTML SEC serves, not the metadata `filings_for_cik` already
        collects) via EDGAR's Archives path -- see `_document_url` and
        `ARCHIVES_BASE`'s own comment for the confirmed URL scheme. NOT
        called automatically for every collected filing -- see
        agent/edgar_collector.py's module docstring for where that decision
        belongs (a filing the T3 screen has already flagged, via the T4
        trigger path, not the periodic metadata sweep).

        `max_bytes` bounds the fetch itself (enforced during the read, not
        sliced off afterward -- see `Transport.request_raw`); pass
        `agent.config.Config.edgar_document_max_bytes`. See
        `FilingDocumentFetch`'s own docstring for what `sha256`/`truncated`
        mean when the cap binds."""
        url = _document_url(cik, accession_number, primary_document)
        raw, truncated = self._get_raw(url, max_bytes=max_bytes)
        return FilingDocumentFetch(
            text=raw.decode("utf-8", errors="replace"),
            sha256=hashlib.sha256(raw).hexdigest(),
            byte_length=len(raw),
            truncated=truncated,
        )

    def ticker_cik_map(self) -> dict[str, str]:
        """`{TICKER (uppercased): 10-digit zero-padded CIK string}`. SEC's
        own file is keyed by an arbitrary numeric index, not by ticker --
        `{"0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."}, ...}`
        (confirmed directly, fetched 2026-07-31) -- re-keyed here into the
        shape every caller in this codebase actually wants."""
        body = self._get(TICKER_CIK_URL)
        out: dict[str, str] = {}
        for entry in body.values():
            ticker = str(entry["ticker"]).upper()
            out[ticker] = str(entry["cik_str"]).zfill(10)
        return out

    def filings_for_cik(self, cik: str, *, forms: frozenset[str] = ALLOWED_FORMS,
                        include_older: bool = True) -> list[dict]:
        """Every filing of a form in `forms` for this CIK, from `filings.
        recent` plus (if `include_older`) every additional file
        `filings.files` names, oldest-and-newest combined in whatever order
        SEC itself returns them (not re-sorted -- a caller that needs a
        specific order sorts it, this method reports what SEC reports)."""
        cik10 = str(cik).zfill(10)
        body = self._get(SUBMISSIONS_URL.format(cik10=cik10))
        filings = _parse_recent_filings(body.get("filings", {}).get("recent", {}))
        if include_older:
            # `filings.files` entries name additional JSON files, assumed
            # (not independently confirmed firsthand -- see module
            # docstring's WHAT THIS CLIENT DOES NOT CONFIRM section for the
            # general posture) to carry the same columnar shape directly at
            # each file's own top level, not re-wrapped in another
            # "filings"/"recent". Only exercised for a filer with more than
            # a year or 1,000 filings -- rare for a small pilot universe,
            # but not assumed away.
            for older in body.get("filings", {}).get("files", []):
                older_body = self._get(SUBMISSIONS_BASE + older["name"])
                filings.extend(_parse_recent_filings(older_body))
        return [f for f in filings if f["form"] in forms]
