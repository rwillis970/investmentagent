"""Real-input wiring for the T3 materiality screen (§2, §11 Day 4 collectors
unit, Commit 4).

Builds real `agent.materiality.MaterialityCandidate`s from stored T1
(`agent.market_data_collector`) and T2 (`agent.edgar_collector`) facts, then
calls the existing, UNCHANGED `agent.materiality.screen()` over each. This
module supplies inputs and glues results together -- it makes no materiality
decision of its own, changes no arithmetic in `agent.materiality`, and calls
no model, directly or indirectly (`screen()` itself already guarantees the
latter; nothing added here changes that -- see this module's own
`test_run_materiality_cycle_makes_zero_model_calls`). NOT wired into `agent.
run_loop` -- exercised directly, per this unit's own scope ("do not run this
from the reconciliation loop yet").

ELIGIBLE_UNIVERSE: SUPPLIED BY `agent.config.Config.symbol_universe`, NOT
RE-FILTERED HERE. Before this commit, nothing in this codebase named a
tradeable symbol set at all (`agent.materiality`'s own module docstring).
`symbol_universe` is `{SYMBOL: asset_class}` -- every symbol this cycle is
given IS, by construction, the eligible universe passed to `screen()`; a
symbol the caller does not want screened at all should not be in
`symbol_universe` to begin with, not filtered out again here.

COOLDOWN_SYMBOLS: SUPPLIED BY THE CALLER, BACKED BY `agent.holding.
symbols_in_cooldown` (ALSO ADDED THIS COMMIT). This module does not compute
cooldown itself -- it takes `cooldown_symbols` as a plain `frozenset[str]`,
exactly as `agent.materiality.screen()` already does, so a caller wires
`symbols_in_cooldown(ledger.lots(), policy_registry, now=now)` in wherever it
has a `Ledger` and `HoldingPolicyRegistry` in hand (this module intentionally
does not import `agent.ledger`, to stay usable in tests -- and eventually in
a T2-only replay -- with no ledger at all).

PEER_MEDIAN_RETURN (feeds `MaterialityCandidate.sector_ret`): MARKET-
RELATIVE, NOT SECTOR-RELATIVE -- RENAMED AND LABELLED, NOT SILENTLY
SUBSTITUTED (REVIEW FIX). No sector-classification or sector-benchmark-
return source exists anywhere in this codebase (Commits 1-2 collect
per-symbol price/volume and EDGAR filings only). The original version of
this module computed a substitute called "sector_ret" and reported it under
that name in `score_components` -- indistinguishable, on paper, from a real
per-GICS-sector return. It is not one: `asset_class` is the CAPABILITY
dimension (`agent.policy.TradeCapabilityPolicy`'s own key -- "US_EQUITY",
"ETF", ...), not a sector classification, so "every other symbol in the
same asset_class within `symbol_universe`" is, in the common case of a
single-asset-class universe, THE ENTIRE UNIVERSE. What this module computes
is therefore a MARKET-relative peer median, not a sector-relative one --
§3.2's own `abs(ret_since_open - sector_ret) / atr_20` is meant to isolate
an IDIOSYNCRATIC move (net of the symbol's sector), and scoring it against
a whole-universe median instead measures a move net of the MARKET: a stock
moving with its sector but against the market would score as idiosyncratic
when it is not, and vice versa. Renamed throughout this module's own code
to `peer_median_ret_since_open` -- nothing here calls it "sector" anymore --
and `run_materiality_cycle` attaches an explicit `data_provenance` entry to
every `OpportunityEvent.score_components` it returns, naming this
substitution plainly, so `raw_terms["sector_ret"]` is self-describing to
whoever reconstructs a decision months later (see PROVENANCE ANNOTATION
below).

MINIMUM PEER-GROUP SIZE: `None` BELOW THRESHOLD, EXACTLY LIKE
`earnings_proximity` (REVIEW FIX). A "median" computed over one peer is
just that one peer's own return relabelled as a cross-sectional statistic;
over two, Python's `statistics.median` is their mean, not a value resistant
to either one being an outlier -- the entire point of taking a median.
Three is the smallest peer count at which "median" behaves as a median
(a real middle value, robust to one outlier) rather than degenerating into
"that one other stock's return" or "the average of two". `min_peer_group_size`
is a REQUIRED parameter here (no hardcoded default in this module -- see
`agent.config.Config.materiality_min_peer_group_size`, added the same
commit, default `3`, per §9.1's same-commit rule); below it,
`sector_ret=None` for that candidate (the candidate is still BUILT, exactly
as `earnings_proximity` already does for insufficient filing history -- see
agent/earnings.py -- never skipped outright for this reason alone).
`agent.materiality.compute_score` is what decides what a `None` here means
for the score (see that module's own UNKNOWN-INPUT RULE comment): under a
zero `materiality_w5`, harmless; under a nonzero one, the candidate is
disqualified from scoring at all, never silently treated as zero.
CONSEQUENCE WORTH FLAGGING: `symbol_universe` is empty by default and will
start small, and `materiality_w5` defaults to a NONZERO 1.0 in
config.example.json -- so with a universe holding fewer than
`min_peer_group_size` (default 3) symbols in the same asset_class, EVERY
candidate's `sector_ret` will be `None` and EVERY candidate will be
disqualified (skipped, never triggering) until the universe grows past
that floor. This is the fail-safe-to-NO-TRADE invariant working as
intended, not a bug -- but it means a small pilot universe produces zero
triggerable events by design, and whoever configures `symbol_universe`
should know that before wondering why nothing ever fires.

FILING CONTEXT: THE MOST RECENT FILING ON RECORD, RE-SCORED EVERY CYCLE
UNTIL A NEWER ONE ARRIVES. A symbol's `form_type`/`item_codes` come from the
latest `"filing"` Fact `agent.edgar_collector` has stored for it as of `now`
(if any); `filing_weight` correctly scores it as zero once nothing on file
is on the material-forms allowlist. There is no "already analysed this
filing" tracker in this codebase yet (T4's own dedup/analysis-status
bookkeeping is explicitly out of scope for this unit) -- so the SAME still-
most-recent filing will produce the SAME candidate, and therefore the SAME
`OpportunityEvent`, on every cycle until a genuinely newer filing supersedes
it. This is a known, disclosed limitation of running this cycle standalone,
repeatedly, with nothing downstream to consume/dedup its output yet -- not a
bug this commit silently introduces.

PROVENANCE ANNOTATION. `run_materiality_cycle` calls `dataclasses.replace`
on the `OpportunityEvent` `screen()` returns, adding a
`score_components["data_provenance"]` key naming the peer-median
substitution -- never mutating `score_components` in place (the event is
frozen, and this module respects that by constructing a new one rather than
reaching into the returned object's nested dict).

FAIL-SAFE PER SYMBOL. A symbol missing a `market_snapshot` fact, or whose
snapshot fails `agent.materiality.compute_score`'s own input validation
(raising `MaterialityInputError` -- e.g. a non-positive `atr_20`, or now an
unknown `sector_ret`/`earnings_proximity` under a live weight -- see that
module's UNKNOWN-INPUT RULE) is skipped for this cycle, not fabricated and
not allowed to abort every OTHER symbol's real result. Insufficient peers
alone no longer skips a symbol -- see MINIMUM PEER-GROUP SIZE above.

SILENT NO-OP VISIBILITY (review round 3, T4 unit Commit 1). The exact
finding from review round 2's own report: with `symbol_universe` empty (its
default) and `materiality_w5` nonzero (its default), every candidate this
pilot could ever build is disqualified under `compute_score`'s UNKNOWN-INPUT
RULE, forever, and the resulting `MaterialityCycleResult` -- zero events,
zero triggers -- is INDISTINGUISHABLE on its face from a healthy cycle that
genuinely found nothing material today. That is the exact failure mode §3.2
exists to prevent: a system that looks like it is working while doing
nothing. Two independent responses, at two different times:

1. RUNTIME (this module, always on): `MaterialityCycleResult.degraded_reason`
   is populated whenever EVERY symbol attempted this cycle was disqualified
   for the IDENTICAL reason (or the universe was empty to begin with) --
   see `_compute_degraded_reason`/`_classify_skip_reason`. This is the
   general case: any uniform-cause failure, not just the w5/peer-count one,
   including e.g. a broker outage that leaves every symbol without a
   `market_snapshot`.

2. STATIC, CONFIG-TIME (`assert_materiality_config_sane`, below): the
   SPECIFIC w5/universe-size interaction is fully knowable from `Config`
   alone, before a single cycle ever runs -- so this function checks it at
   load/startup time, the earliest and cheapest place to catch it. NOT
   wired into `agent.startup.run_startup` in this commit: that function
   does not take a `Config` argument today (confirmed before writing this),
   and threading one through is a larger, separate signature change this
   commit's scope does not ask for -- flagged here as a real, disclosed
   gap for whichever unit next touches startup wiring, not silently
   dropped. Both checks are NON-FATAL (a warning, not a load/config error):
   an empty `symbol_universe` is a valid, common early-pilot state, and the
   goal is diagnosability, not blocking a legitimate configuration.
"""
from __future__ import annotations

import dataclasses
import statistics
from dataclasses import dataclass, field
from datetime import datetime

from .earnings import earnings_proximity as _earnings_proximity
from .edgar_collector import FIELD as _FILING_FIELD
from .edgar_collector import SOURCE_ID as _EDGAR_SOURCE_ID
from .market_data_collector import FIELD as _SNAPSHOT_FIELD
from .market_data_collector import SOURCE_ID as _MARKET_SOURCE_ID
from .market_data_collector import read_market_snapshot
from .materiality import MaterialityCandidate, MaterialityInputError, MaterialityPolicy, screen
from .policy import TradeCapabilityPolicy
from .store import AsOfView, Fact

# See module docstring's PEER_MEDIAN_RETURN section: this is NOT a sector
# return, and this note exists so that fact travels with the number, not
# just with this module's own docstring.
PEER_MEDIAN_PROVENANCE_NOTE = (
    "raw_terms['sector_ret'] is NOT a verified per-GICS-sector return -- no "
    "sector-classification or sector-benchmark source exists in this "
    "codebase. It is peer_median_ret_since_open: the median ret_since_open "
    "across this symbol's peers in symbol_universe sharing the same "
    "asset_class (a capability dimension, e.g. every US_EQUITY symbol) -- "
    "effectively MARKET-relative, not SECTOR-relative, whenever "
    "symbol_universe holds one asset_class group. `None` below the "
    "configured minimum peer-group size (agent.config.Config."
    "materiality_min_peer_group_size). See agent/materiality_cycle.py's "
    "module docstring for the full reasoning."
)


class MaterialityCycleError(Exception):
    pass


@dataclass(frozen=True)
class CandidateBuildResult:
    candidates: tuple[MaterialityCandidate, ...]
    skipped: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class MaterialityCycleResult:
    """REVIEW FIX: `run_materiality_cycle` used to return a bare
    `(events, skipped)` tuple -- the one place in this whole collectors unit
    that did NOT return a named dataclass the way `CandidateBuildResult`/
    `MarketDataCollectionResult`/`EdgarCollectionResult` already do. Folded
    into this same fix because `degraded_reason` (below) needed a third
    slot, and growing a bare tuple to three elements is exactly the kind of
    silent, positional-arg-order footgun a named result exists to avoid.

    `degraded_reason`: see `_classify_skip_reason`/the module docstring's
    SILENT NO-OP VISIBILITY section. `None` in the normal case (some events
    triggered, or a genuinely mixed set of skip reasons) -- a real string
    only when EVERY symbol in the given universe was attempted and skipped
    for the IDENTICAL reason, or when the universe itself was empty. A
    cycle that "ran fine and found nothing material" must not look
    identical to a cycle that never had a chance to find anything at all."""
    events: list
    skipped: dict[str, str] = field(default_factory=dict)
    degraded_reason: str | None = None


# SILENT NO-OP VISIBILITY (review round 3, Commit 1): known, deliberately
# coarse classification of `skipped` messages into a small set of causes,
# so `run_materiality_cycle` can tell "every candidate failed for the SAME
# reason" apart from a normal mixed/partial result. Matching is against
# THIS CODEBASE'S OWN exception message text (agent.materiality/agent.
# materiality_cycle), never against collected/untrusted content -- these
# messages are generated entirely by code in this repo, so this is an
# ordinary maintenance dependency (a message text changing requires
# updating this table), not a parsing risk.
_SKIP_REASON_PATTERNS: tuple[tuple[str, str], ...] = (
    ("no market_snapshot fact available", "NO_MARKET_SNAPSHOT"),
    ("sector_ret is unknown but", "SECTOR_RET_UNKNOWN_UNDER_LIVE_WEIGHT"),
    ("earnings_proximity is unknown but", "EARNINGS_PROXIMITY_UNKNOWN_UNDER_LIVE_WEIGHT"),
    ("atr_20 must be positive", "MALFORMED_ATR_20"),
    ("median_volume_same_time must be positive", "MALFORMED_MEDIAN_VOLUME"),
    ("volume_so_far cannot be negative", "MALFORMED_VOLUME_SO_FAR"),
)


def _classify_skip_reason(message: str) -> str:
    for substring, code in _SKIP_REASON_PATTERNS:
        if substring in message:
            return code
    return "OTHER"


def _compute_degraded_reason(symbol_universe: dict[str, str], events: list,
                             skipped: dict[str, str]) -> str | None:
    if not symbol_universe:
        return ("symbol_universe is empty -- no candidates were attempted "
               "this cycle (this looks identical to a healthy 'nothing "
               "material today' cycle unless surfaced distinctly)")
    if events or not skipped:
        return None
    if len(skipped) != len(symbol_universe):
        return None   # a genuinely mixed result: some symbols triggered/skipped differently
    codes = {_classify_skip_reason(msg) for msg in skipped.values()}
    if len(codes) != 1:
        return None   # skipped for a MIX of reasons -- not the uniform failure this flags
    (only_code,) = codes
    return (
        f"all {len(symbol_universe)} symbol(s) in symbol_universe were "
        f"disqualified this cycle for the SAME reason ({only_code}) -- zero "
        "events were produced. This looks identical to a healthy 'nothing "
        "material today' cycle unless surfaced distinctly (§3.2's own "
        "failure mode this exists to prevent): check whether "
        "materiality_min_peer_group_size/materiality_w4/materiality_w5/"
        "symbol_universe are consistent with each other, or whether "
        "upstream T1/T2 collection is actually running."
    )


def assert_materiality_config_sane(*, materiality_w5: float, symbol_universe: dict[str, str],
                                   min_peer_group_size: int) -> str | None:
    """STATIC, CONFIG-TIME half of SILENT NO-OP VISIBILITY (see module
    docstring) -- callable at load/startup time, before a single cycle
    exists to produce a `MaterialityCycleResult`. Checks the one specific
    interaction fully knowable from `agent.config.Config` alone: a nonzero
    `materiality_w5` (§3.2's peer-median term is live) combined with a
    `symbol_universe` where NO asset_class group can ever reach
    `min_peer_group_size` peers -- guaranteeing `sector_ret=None` for every
    candidate, and therefore (per `agent.materiality.compute_score`'s
    UNKNOWN-INPUT RULE) disqualifying every candidate, forever, regardless
    of what T1/T2 ever collect. Returns a human-readable warning string, or
    `None` if this specific interaction cannot occur. NON-FATAL: an empty
    or small `symbol_universe` is a normal, expected early-pilot state --
    this is diagnostic, not a load-time error (see module docstring for why
    this is not wired into `agent.startup.run_startup` in this commit)."""
    if materiality_w5 == 0:
        return None
    if not symbol_universe:
        return (
            "materiality_w5 is nonzero but symbol_universe is empty -- no "
            "candidate can ever have peers, so sector_ret will be None and "
            "every candidate will be disqualified by compute_score's "
            "UNKNOWN-INPUT RULE. Nothing will ever trigger until "
            "symbol_universe is populated."
        )
    group_sizes: dict[str, int] = {}
    for asset_class in symbol_universe.values():
        group_sizes[asset_class] = group_sizes.get(asset_class, 0) + 1
    largest_peer_count = max(size - 1 for size in group_sizes.values())
    if largest_peer_count < min_peer_group_size:
        return (
            f"materiality_w5 is nonzero but no asset_class group in "
            f"symbol_universe reaches materiality_min_peer_group_size "
            f"({min_peer_group_size}) peers (largest group of same-"
            f"asset_class symbols gives any one member only "
            f"{largest_peer_count} peer(s)) -- sector_ret will be None for "
            "every candidate and every candidate will be disqualified by "
            "compute_score's UNKNOWN-INPUT RULE. Nothing will ever trigger "
            "until symbol_universe grows past this floor within at least "
            "one asset_class."
        )
    return None


def _latest_filing_fact(view: AsOfView, symbol: str) -> Fact | None:
    """The most recent `"filing"` Fact on record for `symbol`, as of `view`
    (look-ahead-safe -- see module docstring's FILING CONTEXT section).
    `AsOfView.history` is ascending by `observed_at` (agent/store.py), so the
    last element is the most recently KNOWABLE filing, not necessarily the
    one with the latest `effective_at`."""
    history = view.history(symbol, _FILING_FIELD)
    return history[-1] if history else None


def build_materiality_candidates(view: AsOfView, symbol_universe: dict[str, str], *,
                                 now: datetime, min_peer_group_size: int
                                 ) -> CandidateBuildResult:
    """One cycle's `MaterialityCandidate`s, built from stored T1/T2 facts
    already in `view`. `symbol_universe`: `{SYMBOL: asset_class}` -- reuse
    `agent.config.Config.symbol_universe` directly rather than re-declaring
    it. `min_peer_group_size`: reuse `agent.config.Config.
    materiality_min_peer_group_size` directly -- see module docstring's
    MINIMUM PEER-GROUP SIZE section for why this has no default here and why
    3 is this codebase's own recommended one. See module docstring for the
    PEER_MEDIAN_RETURN proxy and the fail-safe skip conditions."""
    if now.tzinfo is None:
        raise MaterialityCycleError("now must be a timezone-aware datetime")

    symbols = list(symbol_universe)
    snapshots: dict[str, dict] = {}
    for symbol in symbols:
        snap = read_market_snapshot(view, symbol)
        if snap is not None:
            snapshots[symbol] = snap

    candidates: list[MaterialityCandidate] = []
    skipped: dict[str, str] = {}
    for symbol in symbols:
        snap = snapshots.get(symbol)
        if snap is None:
            skipped[symbol] = "no market_snapshot fact available as of now"
            continue
        asset_class = symbol_universe[symbol]

        peers = [s for s in snapshots
                if s != symbol and symbol_universe[s] == asset_class]
        # `None` below the configured floor -- NOT a skip. Mirrors
        # `earnings_proximity`'s own "insufficient history -> None, still
        # build the candidate" posture (agent/earnings.py); `agent.
        # materiality.compute_score` is what decides what a `None` here
        # means for the score (its own UNKNOWN-INPUT RULE).
        if len(peers) >= min_peer_group_size:
            peer_median_ret_since_open = statistics.median(
                [snapshots[p]["ret_since_open"] for p in peers])
        else:
            peer_median_ret_since_open = None

        filing_fact = _latest_filing_fact(view, symbol)
        form_type = filing_fact.value["form"] if filing_fact else None
        item_codes = (tuple(filing_fact.value["item_codes"]) if filing_fact else ())

        proximity = _earnings_proximity(view, symbol, t=now.date())

        candidates.append(MaterialityCandidate(
            symbol=symbol, asset_class=asset_class,
            ret_since_open=snap["ret_since_open"], atr_20=snap["atr_20"],
            volume_so_far=snap["volume_so_far"],
            median_volume_same_time=snap["median_volume_same_time"],
            sector_ret=peer_median_ret_since_open, earnings_proximity=proximity,
            form_type=form_type, item_codes=item_codes,
        ))
    return CandidateBuildResult(candidates=tuple(candidates), skipped=skipped)


def run_materiality_cycle(view: AsOfView, symbol_universe: dict[str, str], *,
                          policy: MaterialityPolicy,
                          capability_policy: TradeCapabilityPolicy, live: bool,
                          analyses_today: int, max_model_analyses_per_day: int,
                          approvals_today: int, max_approval_requests_per_day: int,
                          cooldown_symbols: frozenset[str], now: datetime,
                          min_peer_group_size: int,
                          held_symbols: frozenset[str] = frozenset()):
    """One full T3 cycle: build real candidates (see
    `build_materiality_candidates`) and call the existing, unchanged
    `agent.materiality.screen()` over each. Returns a `MaterialityCycleResult`
    -- `events` a list of `agent.entities.OpportunityEvent`, `skipped` merging
    build-time skips with any candidate `screen()` itself refused to score
    (`MaterialityInputError`, e.g. a non-positive `atr_20`, or now an
    unknown `sector_ret`/`earnings_proximity` under a live weight -- caught
    and skipped per-symbol, not raised, matching every other per-symbol
    failure in this unit), and `degraded_reason` -- `None` normally, a real
    string when every attempted symbol was disqualified for the identical
    reason (see `MaterialityCycleResult`'s own docstring and this module's
    SILENT NO-OP VISIBILITY section: a healthy "nothing material today"
    cycle must not be indistinguishable from a misconfigured one).

    `min_peer_group_size`: reuse `agent.config.Config.
    materiality_min_peer_group_size` directly -- see module docstring's
    MINIMUM PEER-GROUP SIZE section.

    `held_symbols` (REVIEW FIX, Commit 5): `agent.materiality.screen()`'s
    capability check used to hardcode `side="BUY"` unconditionally, so a
    material event on a symbol this account already HOLDS -- where the
    warranted action is an exit, i.e. a SELL -- would be wrongly evaluated
    against BUY's own capability status. A symbol in `held_symbols` is
    screened with `side="SELL"`; every other symbol keeps `screen()`'s own
    default of `"BUY"`. Plain `frozenset[str]`, exactly like
    `cooldown_symbols` -- this module deliberately does not import `agent.
    ledger` (see module docstring), so a caller wires
    `frozenset(ledger.positions())` in wherever it has a `Ledger` in hand.

    Every returned event carries a `score_components["data_provenance"]`
    entry naming the `sector_ret`/peer-median substitution (see
    `PEER_MEDIAN_PROVENANCE_NOTE` above) -- attached via `dataclasses.
    replace`, never by mutating the frozen event's nested dict in place.

    Not persisted anywhere: there is no `OpportunityEvent` store in this
    codebase yet (verified before writing this -- building one is a later,
    separate unit's job, alongside T4's own analysis/dedup bookkeeping)."""
    built = build_materiality_candidates(view, symbol_universe, now=now,
                                         min_peer_group_size=min_peer_group_size)
    eligible_universe = frozenset(symbol_universe)

    events = []
    skipped = dict(built.skipped)
    for candidate in built.candidates:
        filing_fact = _latest_filing_fact(view, candidate.symbol)
        if filing_fact is not None:
            event_type, source_id = "FILING", _EDGAR_SOURCE_ID
            observed_at, effective_at = filing_fact.observed_at, filing_fact.effective_at
        else:
            event_type, source_id = "PRICE_MOVE", _MARKET_SOURCE_ID
            observed_at = effective_at = now
        event_id = f"{source_id}:{candidate.symbol}:{observed_at.isoformat()}"
        side = "SELL" if candidate.symbol in held_symbols else "BUY"

        try:
            event = screen(
                candidate, policy=policy, capability_policy=capability_policy, live=live,
                analyses_today=analyses_today,
                max_model_analyses_per_day=max_model_analyses_per_day,
                approvals_today=approvals_today,
                max_approval_requests_per_day=max_approval_requests_per_day,
                eligible_universe=eligible_universe, cooldown_symbols=cooldown_symbols,
                event_id=event_id, event_type=event_type, source_id=source_id,
                observed_at=observed_at, effective_at=effective_at, side=side,
            )
        except MaterialityInputError as exc:
            skipped[candidate.symbol] = str(exc)
            continue
        event = dataclasses.replace(event, score_components={
            **event.score_components,
            "data_provenance": {"sector_ret": PEER_MEDIAN_PROVENANCE_NOTE},
        })
        events.append(event)
    degraded_reason = _compute_degraded_reason(symbol_universe, events, skipped)
    return MaterialityCycleResult(events=events, skipped=skipped,
                                  degraded_reason=degraded_reason)
