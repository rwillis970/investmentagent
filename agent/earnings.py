"""§3.2 `earnings_proximity(t)` (§2, §11 Day 4 collectors unit, Commit 3).

NO FREE FORWARD-LOOKING EARNINGS CALENDAR EXISTS. Confirmed directly during
this unit's research: Alpaca's Market Data API tree (the same account this
codebase already holds paper credentials for) has no fundamentals/earnings-
calendar endpoint anywhere under `data.alpaca.markets`, and SEC EDGAR is a
disclosure system, not a scheduling one -- it records that an earnings
release HAPPENED (an 8-K carrying item 2.02, "Results of Operations and
Financial Condition") only after the fact; nothing in EDGAR states when a
company's NEXT earnings release will occur. A paid data vendor could supply
this, but none is wired into this pilot, and inventing a number here would
violate this unit's own instruction to stop and report rather than invent.

WHAT THIS MODULE DOES INSTEAD: AN ESTIMATED, BACKWARD-DERIVED CADENCE. §3.2
writes `earnings_proximity(t)` as a bare function with no specified
direction -- it does not say the input must be a confirmed forward date.
Commit 2's EDGAR collector already stores every item-2.02-bearing 8-K a
symbol has filed, which means each symbol's OWN HISTORICAL REPORTING
CADENCE (almost every US public company reports quarterly, on a fairly
consistent ~90-day cycle) is derivable today, with no new data source, from
facts this pilot already collects. `earnings_release_dates` reads that
history; `compute_earnings_proximity` takes the median interval between a
symbol's own past releases, projects one ESTIMATED next release date from
it, and returns a value that peaks at 1.0 exactly on that estimated date and
decays linearly to 0.0 within `_PROXIMITY_HALF_WINDOW_DAYS` either side.

THIS IS AN ESTIMATE, NOT A SCHEDULE, AND IS WEAKER THAN A TRUE CALENDAR. A
company can report early or late relative to its own historical cadence
(especially around holidays, fiscal-year-end changes, or a delayed filing),
and the estimate only re-centers once a NEW item-2.02 8-K is actually
observed -- until then, once `t` passes the estimated date, proximity decays
to and stays at 0.0, which understates true proximity if the real release is
simply late. This is the reason `materiality_w4` is set to `0.0` in
`config.example.json` (see that file's own comment): the term stays real and
plumbed through end to end, but its contribution to the score is switched
off until it can be calibrated against replayed history, exactly the same
"uncalibrated placeholder" posture `materiality_w1`-`w6` already hold
themselves to for the weights this unit did not touch.

`None`, NEVER `0.0`, WHEN NO ESTIMATE CAN BE MADE. Fewer than two prior
earnings releases on record for a symbol means there is no interval to take
a median of, and therefore no cadence to estimate from -- this is the "we
don't know" case, and `earnings_proximity` returns `None` to keep it
distinguishable from "we know, and it's exactly a half-window edge case"
(which is a real `0.0`). `agent.materiality.compute_score` treats a `None`
here as contributing zero to the score's arithmetic (which `w4=0.0` already
guarantees regardless), while preserving the actual `None` in
`score_components["raw_terms"]` so a human auditing a screen decision later
can see the difference. See that module's own comment at the point it
consumes this value for the one caveat this raises: once `w4` is ever
calibrated away from `0.0`, a `None` here will silently score as "no
proximity" rather than "unknown" -- a real gap, deliberately left open here
rather than resolved by guessing what "unknown" should do to a materiality
score (that decision belongs with whoever calibrates `w4`, not with this
commit).

CALENDAR-DAY ARITHMETIC, NOT TRADING-DAY. Earnings cadence spans weekends
and holidays uniformly (a company reporting every ~91 calendar days does not
care that some of those days are non-trading days), and using `agent.
market_calendar`'s trading-day machinery here would add a dependency on its
2024-2028 coverage window for no benefit this term needs.
"""
from __future__ import annotations

import statistics
from datetime import date, timedelta

from .edgar_collector import FIELD as _FILING_FIELD
from .store import AsOfView

# SEC's own item-code table: Item 2.02, "Results of Operations and Financial
# Condition" -- the 8-K item that accompanies an earnings release. Reused
# from `agent.materiality.MATERIAL_8K_ITEMS` in spirit (that allowlist names
# every materially-weighted item; this module only cares about the one that
# specifically marks an earnings event), named locally so this module does
# not import a private detail of materiality's own allowlist shape.
EARNINGS_RELEASE_ITEM = "2.02"

# Need at least two past releases to take a median interval between them --
# one release alone names a date but not a cadence.
_MIN_RELEASES_FOR_ESTIMATE = 2

# UNCALIBRATED. How many days either side of the estimated next release date
# still count as "proximate", decaying linearly from 1.0 at the estimate
# itself to 0.0 at the edge. Same posture as materiality_w1-w6: a real,
# documented placeholder pending calibration against replayed history, not a
# guess dressed up as a constant. Zero effect on the score today regardless
# of this value's choice, since materiality_w4 is 0.0 until calibrated.
_PROXIMITY_HALF_WINDOW_DAYS = 5


def earnings_release_dates(view: AsOfView, symbol: str) -> list[date]:
    """Every known earnings-release date for `symbol`: the `effective_at`
    date (the period the filing describes, not when it became knowable --
    see `agent.edgar_collector`'s own EFFECTIVE_AT note) of every stored
    `"filing"` Fact whose form is 8-K and whose `item_codes` include
    `EARNINGS_RELEASE_ITEM`. Sorted oldest-first, deduplicated by date.
    Respects the store's own look-ahead guard via `view` -- a fact with
    `observed_at` after `view.as_of` simply is not in `view.history(...)`."""
    dates: set[date] = set()
    for fact in view.history(symbol, _FILING_FIELD):
        value = fact.value
        if value.get("form") != "8-K":
            continue
        if EARNINGS_RELEASE_ITEM not in (value.get("item_codes") or ()):
            continue
        dates.add(fact.effective_at.date())
    return sorted(dates)


def compute_earnings_proximity(release_dates: list[date], *, t: date) -> float | None:
    """The pure arithmetic half: given a symbol's known past earnings-release
    dates, estimate its next release from the median interval between
    consecutive past releases, and return a proximity in `[0.0, 1.0]` that
    peaks at the estimate and decays linearly to `0.0` within
    `_PROXIMITY_HALF_WINDOW_DAYS` either side. Returns `None` (see module
    docstring) when fewer than `_MIN_RELEASES_FOR_ESTIMATE` releases are
    given -- there is no interval to estimate a cadence from."""
    if len(release_dates) < _MIN_RELEASES_FOR_ESTIMATE:
        return None
    ordered = sorted(release_dates)
    intervals = [(b - a).days for a, b in zip(ordered, ordered[1:])]
    median_interval_days = statistics.median(intervals)
    estimated_next = ordered[-1] + timedelta(days=median_interval_days)
    distance_days = abs((t - estimated_next).days)
    if distance_days >= _PROXIMITY_HALF_WINDOW_DAYS:
        return 0.0
    return 1.0 - (distance_days / _PROXIMITY_HALF_WINDOW_DAYS)


def earnings_proximity(view: AsOfView, symbol: str, *, t: date) -> float | None:
    """§3.2's `earnings_proximity(t)` -- the estimated-cadence half only (see
    module docstring for why no forward-looking half exists). Reads
    `symbol`'s known release history from `view` (preserving the store's
    look-ahead guard) and estimates proximity to a projected next release."""
    return compute_earnings_proximity(earnings_release_dates(view, symbol), t=t)
