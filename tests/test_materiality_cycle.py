"""agent/materiality_cycle.py (§2, §11 Day 4 collectors unit, Commit 4):
real-input wiring for the T3 materiality screen. Builds real
`MaterialityCandidate`s from stored T1 (`agent.market_data_collector`) and
T2 (`agent.edgar_collector`) facts, and calls the existing, UNCHANGED
`agent.materiality.screen()` over each -- no model call anywhere, and the
screen stays deterministic (this unit's own instruction). NOT wired into
`agent.run_loop` -- this module is exercised directly, exactly as
instructed ("do not run this from the reconciliation loop yet").
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone

import pytest

from agent.edgar_collector import FIELD as FILING_FIELD
from agent.edgar_collector import SOURCE_ID as EDGAR_SOURCE_ID
from agent.market_data_collector import FIELD as SNAPSHOT_FIELD
from agent.market_data_collector import SOURCE_ID as MARKET_SOURCE_ID
from agent.materiality import DEFAULT_FILING_WEIGHTS, MaterialityInputError, MaterialityPolicy
from agent.materiality_cycle import (MaterialityCycleError, MaterialityCycleResult,
                                     assert_materiality_config_sane,
                                     build_materiality_candidates,
                                     run_materiality_cycle)
from agent.policy import initial_policy
from agent.store import Fact, FactStore

T0 = datetime(2026, 7, 31, 15, 0, tzinfo=timezone.utc)
CAPS = initial_policy()
# w4 (earnings_proximity) AND w5 (idiosyncratic-vs-peer-median) are both
# zeroed in this suite's base POLICY, same reasoning both times: neither
# term's input can be relied on to be a real number for an arbitrary
# synthetic fixture (earnings_proximity needs 2+ prior filings;
# `sector_ret` -- really a peer-median substitute, see agent/
# materiality_cycle.py's PEER_MEDIAN_RETURN section -- needs
# `min_peer_group_size` peers), and under agent.materiality.compute_score's
# UNKNOWN-INPUT RULE a nonzero weight over an unknown input DISQUALIFIES
# the candidate entirely. Tests that specifically exercise the peer-median
# floor use their own dedicated nonzero-w5 policy below.
POLICY = MaterialityPolicy(version="mat-v1", w1=1.0, w2=1.0, w3=1.0, w4=0.0,
                           w5=0.0, w6=1.0, threshold=2.0,
                           filing_weights=DEFAULT_FILING_WEIGHTS)


def snapshot_fact(symbol, *, ret_since_open=0.0, atr_20=1.0, volume_so_far=100.0,
                  median_volume_same_time=100.0, observed_at=T0):
    return Fact(entity_id=symbol, field=SNAPSHOT_FIELD,
               value={"atr_20": atr_20, "ret_since_open": ret_since_open,
                     "volume_so_far": volume_so_far,
                     "median_volume_same_time": median_volume_same_time,
                     "current_price": 100.0},
               observed_at=observed_at, effective_at=observed_at, source_id=MARKET_SOURCE_ID)


def filing_fact(symbol, *, form="8-K", item_codes=("2.02",), observed_at=T0,
               effective_at=T0, accession="0001"):
    return Fact(entity_id=symbol, field=FILING_FIELD,
               value={"cik": "0000000001", "form": form, "item_codes": list(item_codes),
                     "accession_number": accession, "primary_document": "doc.htm",
                     "filing_date": effective_at.date().isoformat(),
                     "report_date": effective_at.date().isoformat()},
               observed_at=observed_at, effective_at=effective_at,
               source_id=EDGAR_SOURCE_ID, source_doc_hash=accession)


UNIVERSE = {"AAPL": "US_EQUITY", "MSFT": "US_EQUITY"}


def run_cycle(store, universe=UNIVERSE, **over):
    kw = dict(policy=POLICY, capability_policy=CAPS, live=True,
              analyses_today=0, max_model_analyses_per_day=8,
              approvals_today=0, max_approval_requests_per_day=4,
              cooldown_symbols=frozenset(), now=T0, min_peer_group_size=3)
    kw.update(over)
    return run_materiality_cycle(store.now_view(), universe, **kw)


def build_candidates(store, universe=UNIVERSE, *, now=T0, min_peer_group_size=3):
    return build_materiality_candidates(store.now_view(), universe, now=now,
                                        min_peer_group_size=min_peer_group_size)


# ------------------------------------------------------- build_materiality_candidates

def test_rejects_a_naive_now():
    store = FactStore()
    with pytest.raises(MaterialityCycleError):
        build_candidates(store, now=datetime(2026, 7, 31))


def test_symbol_with_no_snapshot_is_skipped():
    universe = {"AAPL": "US_EQUITY", "MSFT": "US_EQUITY", "GOOG": "US_EQUITY", "AMZN": "US_EQUITY"}
    store = FactStore()
    store.append(snapshot_fact("AAPL"))
    store.append(snapshot_fact("GOOG"))
    store.append(snapshot_fact("AMZN"))   # AAPL's peers, so AAPL itself builds fine
    result = build_candidates(store, universe=universe)
    symbols = {c.symbol for c in result.candidates}
    assert symbols == {"AAPL", "GOOG", "AMZN"}
    assert "MSFT" in result.skipped
    assert "no market_snapshot" in result.skipped["MSFT"]


# --------------------------------------------- peer-median floor (review round 2)
# REVIEW FIX: a symbol with fewer than `min_peer_group_size` peers used to be
# SKIPPED entirely. It is now built normally with `sector_ret=None` --
# exactly like `earnings_proximity` already does for insufficient filing
# history -- because `agent.materiality.compute_score`'s UNKNOWN-INPUT RULE,
# not this module, is what decides whether that `None` disqualifies a score.

def test_below_the_peer_floor_the_candidate_is_built_with_sector_ret_none_not_skipped():
    store = FactStore()
    store.append(snapshot_fact("AAPL"))
    store.append(snapshot_fact("MSFT"))   # only 1 peer each; floor defaults to 3
    result = build_candidates(store, universe=UNIVERSE)
    by_symbol = {c.symbol: c for c in result.candidates}
    assert by_symbol["AAPL"].sector_ret is None
    assert by_symbol["MSFT"].sector_ret is None
    assert result.skipped == {}


def test_with_zero_peers_the_candidate_is_still_built_with_sector_ret_none():
    store = FactStore()
    store.append(snapshot_fact("AAPL"))
    result = build_candidates(store, universe={"AAPL": "US_EQUITY"})
    assert len(result.candidates) == 1
    assert result.candidates[0].sector_ret is None
    assert result.skipped == {}


def test_at_or_above_the_peer_floor_a_real_median_is_computed_not_a_gics_return():
    universe = {"AAPL": "US_EQUITY", "MSFT": "US_EQUITY", "GOOG": "US_EQUITY", "AMZN": "US_EQUITY"}
    store = FactStore()
    store.append(snapshot_fact("AAPL", ret_since_open=0.05))
    store.append(snapshot_fact("MSFT", ret_since_open=0.02))
    store.append(snapshot_fact("GOOG", ret_since_open=0.03))
    store.append(snapshot_fact("AMZN", ret_since_open=0.10))
    result = build_candidates(store, universe=universe)
    by_symbol = {c.symbol: c for c in result.candidates}
    # AAPL's 3 peers are MSFT/GOOG/AMZN (0.02, 0.03, 0.10) -> median 0.03
    assert by_symbol["AAPL"].sector_ret == 0.03


def test_min_peer_group_size_is_configurable_not_hardcoded():
    """With the floor lowered to 1, a single peer is enough to produce a
    real value -- proves the floor is a real parameter, not a fixed
    constant baked into this module."""
    store = FactStore()
    store.append(snapshot_fact("AAPL", ret_since_open=0.05))
    store.append(snapshot_fact("MSFT", ret_since_open=0.02))
    result = build_candidates(store, universe=UNIVERSE, min_peer_group_size=1)
    by_symbol = {c.symbol: c for c in result.candidates}
    assert by_symbol["AAPL"].sector_ret == 0.02


def test_candidate_carries_the_latest_filings_form_and_item_codes():
    store = FactStore()
    store.append(snapshot_fact("AAPL"))
    store.append(snapshot_fact("MSFT"))
    store.append(filing_fact("AAPL", form="8-K", item_codes=("2.02", "9.01")))
    result = build_candidates(store)
    by_symbol = {c.symbol: c for c in result.candidates}
    assert by_symbol["AAPL"].form_type == "8-K"
    assert by_symbol["AAPL"].item_codes == ("2.02", "9.01")
    assert by_symbol["MSFT"].form_type is None
    assert by_symbol["MSFT"].item_codes == ()


def test_candidate_uses_the_most_recent_filing_when_more_than_one_exists():
    store = FactStore()
    store.append(snapshot_fact("AAPL"))
    store.append(snapshot_fact("MSFT"))
    store.append(filing_fact("AAPL", form="10-Q", observed_at=T0, accession="0001"))
    store.append(filing_fact("AAPL", form="8-K", item_codes=("5.02",),
                             observed_at=datetime(2026, 7, 31, 16, 0, tzinfo=timezone.utc),
                             accession="0002"))
    result = build_candidates(store, now=T0 + timedelta(hours=2))
    by_symbol = {c.symbol: c for c in result.candidates}
    assert by_symbol["AAPL"].form_type == "8-K"
    assert by_symbol["AAPL"].item_codes == ("5.02",)


def test_candidate_earnings_proximity_is_none_without_two_prior_releases():
    store = FactStore()
    store.append(snapshot_fact("AAPL"))
    store.append(snapshot_fact("MSFT"))
    result = build_candidates(store)
    by_symbol = {c.symbol: c for c in result.candidates}
    assert by_symbol["AAPL"].earnings_proximity is None


def test_respects_the_look_ahead_guard_via_the_supplied_view():
    store = FactStore()
    store.append(snapshot_fact("AAPL", observed_at=T0))
    store.append(snapshot_fact("MSFT", observed_at=T0))
    before = store.as_of(T0 - timedelta(seconds=1))
    result = build_materiality_candidates(before, UNIVERSE, now=T0, min_peer_group_size=3)
    assert result.candidates == ()
    assert "AAPL" in result.skipped and "MSFT" in result.skipped


# ------------------------------------------------------------- run_materiality_cycle

def test_a_symbol_with_a_new_filing_produces_a_filing_typed_event():
    store = FactStore()
    store.append(snapshot_fact("AAPL", ret_since_open=5.0, atr_20=0.1))
    store.append(snapshot_fact("MSFT", ret_since_open=0.0, atr_20=1.0))
    store.append(filing_fact("AAPL", form="8-K", item_codes=("2.02",),
                             observed_at=T0, effective_at=T0))
    result = run_cycle(store)
    events = result.events
    by_symbol = {e.symbols[0]: e for e in events}
    assert by_symbol["AAPL"].source_id == EDGAR_SOURCE_ID
    assert by_symbol["AAPL"].observed_at == T0
    assert by_symbol["MSFT"].source_id == MARKET_SOURCE_ID
    assert result.degraded_reason is None


# ---------------------------------- peer-median provenance (review round 2)

def test_every_event_carries_a_data_provenance_note_for_sector_ret():
    """§3.2's raw_terms['sector_ret'] must be self-describing months later:
    every event this cycle produces (regardless of whether a real peer
    median or a None was computed) says plainly that it is a peer-median
    substitute, not a verified sector return."""
    store = FactStore()
    store.append(snapshot_fact("AAPL", ret_since_open=5.0, atr_20=0.1))
    store.append(snapshot_fact("MSFT", ret_since_open=0.0, atr_20=1.0))
    events = run_cycle(store).events
    for event in events:
        note = event.score_components["data_provenance"]["sector_ret"]
        assert "NOT a verified per-GICS-sector return" in note
        assert "peer_median_ret_since_open" in note


def test_a_nonzero_w5_with_insufficient_peers_disqualifies_the_candidate():
    """REVIEW FIX: below the peer floor, sector_ret is None: under a
    NONZERO w5 (unlike this suite's base POLICY), compute_score's
    UNKNOWN-INPUT RULE must refuse to score the candidate at all --
    disqualified (skipped), not silently treated as zero."""
    nonzero_w5_policy = MaterialityPolicy(version="mat-w5-live", w1=1.0, w2=1.0, w3=1.0,
                                          w4=0.0, w5=1.0, w6=1.0, threshold=2.0,
                                          filing_weights=DEFAULT_FILING_WEIGHTS)
    store = FactStore()
    store.append(snapshot_fact("AAPL", ret_since_open=5.0, atr_20=0.1))
    store.append(snapshot_fact("MSFT", ret_since_open=0.0, atr_20=1.0))
    # Only 1 peer each -- below the default floor of 3.
    result = run_cycle(store, policy=nonzero_w5_policy)
    events, skipped = result.events, result.skipped
    assert events == []
    assert "AAPL" in skipped and "sector_ret is unknown" in skipped["AAPL"]
    assert "MSFT" in skipped and "sector_ret is unknown" in skipped["MSFT"]
    # every symbol disqualified for the SAME reason -> degraded, not a
    # silent "nothing material today" empty result (SILENT NO-OP VISIBILITY)
    assert result.degraded_reason is not None
    assert "SECTOR_RET_UNKNOWN_UNDER_LIVE_WEIGHT" in result.degraded_reason


def test_a_nonzero_w5_with_enough_peers_scores_normally():
    universe = {"AAPL": "US_EQUITY", "MSFT": "US_EQUITY", "GOOG": "US_EQUITY", "AMZN": "US_EQUITY"}
    nonzero_w5_policy = MaterialityPolicy(version="mat-w5-live", w1=1.0, w2=1.0, w3=1.0,
                                          w4=0.0, w5=1.0, w6=1.0, threshold=2.0,
                                          filing_weights=DEFAULT_FILING_WEIGHTS)
    store = FactStore()
    store.append(snapshot_fact("AAPL", ret_since_open=5.0, atr_20=0.1))
    store.append(snapshot_fact("MSFT", ret_since_open=0.0, atr_20=1.0))
    store.append(snapshot_fact("GOOG", ret_since_open=0.01, atr_20=1.0))
    store.append(snapshot_fact("AMZN", ret_since_open=0.02, atr_20=1.0))
    result = run_cycle(store, universe=universe, policy=nonzero_w5_policy)
    events, skipped = result.events, result.skipped
    assert {e.symbols[0] for e in events} == {"AAPL", "MSFT", "GOOG", "AMZN"}
    assert skipped == {}
    assert result.degraded_reason is None


def test_eligible_universe_is_exactly_the_symbol_universe_given():
    store = FactStore()
    store.append(snapshot_fact("AAPL", ret_since_open=5.0, atr_20=0.1))
    store.append(snapshot_fact("MSFT", ret_since_open=0.0, atr_20=1.0))
    events = run_cycle(store).events
    by_symbol = {e.symbols[0]: e for e in events}
    assert by_symbol["AAPL"].score_components["gates"]["in_eligible_universe"] is True


def test_cooldown_symbols_are_passed_through_to_the_screen():
    store = FactStore()
    store.append(snapshot_fact("AAPL", ret_since_open=5.0, atr_20=0.1))
    store.append(snapshot_fact("MSFT", ret_since_open=0.0, atr_20=1.0))
    events = run_cycle(store, cooldown_symbols=frozenset({"AAPL"})).events
    by_symbol = {e.symbols[0]: e for e in events}
    assert by_symbol["AAPL"].analysis_status == "SUPPRESSED"
    assert by_symbol["AAPL"].suppressed_reason == "in_cooldown"


def test_a_symbol_skipped_at_candidate_build_time_is_also_reported_skipped_here():
    universe = {"AAPL": "US_EQUITY", "MSFT": "US_EQUITY", "GOOG": "US_EQUITY"}
    store = FactStore()
    store.append(snapshot_fact("AAPL", ret_since_open=5.0, atr_20=0.1))
    store.append(snapshot_fact("GOOG", ret_since_open=0.0, atr_20=1.0))
    # MSFT has no snapshot at all
    result = run_cycle(store, universe=universe)
    events, skipped = result.events, result.skipped
    assert {e.symbols[0] for e in events} == {"AAPL", "GOOG"}
    assert "MSFT" in skipped
    assert result.degraded_reason is None   # a genuinely mixed result, not uniform


def test_a_malformed_snapshot_is_skipped_not_raised():
    """A non-positive atr_20 would make compute_score raise
    MaterialityInputError -- caught and skipped per-symbol here, the same
    fail-safe posture every other collector in this unit already holds
    itself to, rather than aborting the whole cycle for every other symbol."""
    store = FactStore()
    store.append(snapshot_fact("AAPL", atr_20=0.0))
    store.append(snapshot_fact("MSFT", ret_since_open=0.0, atr_20=1.0))
    result = run_cycle(store)
    events, skipped = result.events, result.skipped
    assert {e.symbols[0] for e in events} == {"MSFT"}
    assert "AAPL" in skipped
    assert result.degraded_reason is None   # MSFT triggered fine -- not a uniform failure


# --------------------------------------------------- held_symbols (Commit 5)
# REVIEW FIX: `screen()`'s capability check used to hardcode `side="BUY"`,
# so a material event on a symbol this account already holds could be
# wrongly evaluated against BUY's own capability status rather than SELL's.
# `held_symbols` is this cycle's own source of "which side to check" --
# analogous to `cooldown_symbols`, a plain caller-supplied set (this module
# does not import `agent.ledger` -- see module docstring).

def test_a_held_symbol_is_screened_as_a_sell_not_a_buy():
    from agent.policy import CapabilityStatus, TradeCapabilityPolicy
    caps = TradeCapabilityPolicy(
        version="sell-disabled-test",
        asset_class={"US_EQUITY": CapabilityStatus.PRODUCTION_ALLOWED},
        side={"BUY": CapabilityStatus.PRODUCTION_ALLOWED,
             "SELL": CapabilityStatus.DISABLED},
        funding={"SETTLED_CASH": CapabilityStatus.PRODUCTION_ALLOWED},
    )
    store = FactStore()
    store.append(snapshot_fact("AAPL", ret_since_open=5.0, atr_20=0.1))
    store.append(snapshot_fact("MSFT", ret_since_open=0.0, atr_20=1.0))
    events = run_cycle(store, capability_policy=caps, held_symbols=frozenset({"AAPL"})).events
    by_symbol = {e.symbols[0]: e for e in events}
    # AAPL is held -> screened as SELL -> SELL is disabled -> suppressed
    assert by_symbol["AAPL"].analysis_status == "SUPPRESSED"
    assert by_symbol["AAPL"].suppressed_reason == "capability_denied"
    # MSFT is not held -> screened as BUY -> BUY is allowed -> not suppressed for capability
    assert by_symbol["MSFT"].score_components["gates"]["capability_allowed"] is True


def test_held_symbols_defaults_to_empty_preserving_existing_behaviour():
    store = FactStore()
    store.append(snapshot_fact("AAPL", ret_since_open=5.0, atr_20=0.1))
    store.append(snapshot_fact("MSFT", ret_since_open=0.0, atr_20=1.0))
    events = run_cycle(store).events
    by_symbol = {e.symbols[0]: e for e in events}
    assert by_symbol["AAPL"].score_components["gates"]["capability_allowed"] is True


def test_run_materiality_cycle_makes_zero_model_calls():
    class Poison:
        def __getattr__(self, name):
            raise AssertionError(f"materiality_cycle touched model-client attribute {name!r}")

        def __call__(self, *a, **kw):
            raise AssertionError("materiality_cycle invoked something callable on the model client")

    poisoned = {"anthropic": Poison(), "agent.llm": Poison(), "agent.model": Poison()}
    saved = {k: sys.modules.get(k) for k in poisoned}
    sys.modules.update(poisoned)
    try:
        store = FactStore()
        store.append(snapshot_fact("AAPL", ret_since_open=5.0, atr_20=0.1))
        store.append(snapshot_fact("MSFT", ret_since_open=0.0, atr_20=1.0))
        store.append(filing_fact("AAPL"))
        run_cycle(store)
    finally:
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v


# --------------------------- SILENT NO-OP VISIBILITY (T4 unit, Commit 1) ---
# `run_materiality_cycle` used to return a bare `(events, skipped)` tuple; it
# now returns `MaterialityCycleResult`, whose third field `degraded_reason`
# distinguishes "a healthy cycle that genuinely found nothing material
# today" from "every candidate was disqualified for the same reason and
# nothing could ever have triggered." See agent/materiality_cycle.py's own
# SILENT NO-OP VISIBILITY docstring section.

def test_degraded_reason_is_none_for_a_healthy_mixed_cycle():
    """Some triggered, some skipped for DIFFERENT reasons -> not degraded."""
    store = FactStore()
    store.append(snapshot_fact("AAPL", ret_since_open=5.0, atr_20=0.1))
    # MSFT has no snapshot at all -- a different failure than any live weight
    result = run_cycle(store, universe={"AAPL": "US_EQUITY", "MSFT": "US_EQUITY"})
    assert result.events != []
    assert result.degraded_reason is None


def test_degraded_reason_is_set_when_symbol_universe_is_empty():
    """`symbol_universe` empty (the shipped default) is the most acute case
    of the failure mode: no candidate is ever even attempted."""
    store = FactStore()
    result = run_cycle(store, universe={})
    assert result.events == []
    assert result.skipped == {}
    assert result.degraded_reason is not None
    assert "symbol_universe is empty" in result.degraded_reason


def test_degraded_reason_is_set_when_every_symbol_fails_for_the_identical_reason():
    """Every symbol in the universe skipped for the SAME classified reason
    (here: no market_snapshot at all) -> degraded, not a normal empty
    result."""
    store = FactStore()
    result = run_cycle(store, universe=UNIVERSE)   # no facts appended at all
    assert result.events == []
    assert set(result.skipped) == {"AAPL", "MSFT"}
    assert result.degraded_reason is not None
    assert "NO_MARKET_SNAPSHOT" in result.degraded_reason


def test_degraded_reason_is_none_when_skip_reasons_are_genuinely_mixed():
    """One symbol skipped for a malformed snapshot, another for having none
    at all -- two DIFFERENT classified reasons -> not degraded (this is not
    the uniform-cause failure mode the check exists to catch)."""
    store = FactStore()
    store.append(snapshot_fact("AAPL", atr_20=0.0))   # malformed -> MALFORMED_ATR_20
    # MSFT: no snapshot fact at all -> NO_MARKET_SNAPSHOT
    result = run_cycle(store, universe=UNIVERSE)
    assert result.events == []
    assert set(result.skipped) == {"AAPL", "MSFT"}
    assert result.degraded_reason is None


def test_degraded_reason_is_none_when_at_least_one_event_triggers():
    """Even if every OTHER symbol was skipped for the same reason, a single
    real event this cycle means it is not a uniform no-op."""
    universe = {"AAPL": "US_EQUITY", "MSFT": "US_EQUITY", "GOOG": "US_EQUITY"}
    store = FactStore()
    store.append(snapshot_fact("AAPL", ret_since_open=5.0, atr_20=0.1))
    # MSFT and GOOG have no snapshot at all
    result = run_cycle(store, universe=universe)
    assert {e.symbols[0] for e in result.events} == {"AAPL"}
    assert set(result.skipped) == {"MSFT", "GOOG"}
    assert result.degraded_reason is None


# ------------------------------------------ assert_materiality_config_sane
# STATIC, CONFIG-TIME half of the same finding: fully knowable from
# `agent.config.Config` values alone, before a single cycle ever runs.

def test_config_sane_is_none_when_w5_is_zero_regardless_of_universe():
    assert assert_materiality_config_sane(
        materiality_w5=0.0, symbol_universe={}, min_peer_group_size=3) is None
    assert assert_materiality_config_sane(
        materiality_w5=0.0, symbol_universe={"AAPL": "US_EQUITY"},
        min_peer_group_size=3) is None


def test_config_sane_warns_on_empty_universe_with_live_w5():
    warning = assert_materiality_config_sane(
        materiality_w5=1.0, symbol_universe={}, min_peer_group_size=3)
    assert warning is not None
    assert "symbol_universe is empty" in warning


def test_config_sane_warns_when_no_asset_class_group_reaches_the_floor():
    # 2 symbols in the same asset_class -> 1 peer each -- below floor of 3
    universe = {"AAPL": "US_EQUITY", "MSFT": "US_EQUITY"}
    warning = assert_materiality_config_sane(
        materiality_w5=1.0, symbol_universe=universe, min_peer_group_size=3)
    assert warning is not None
    assert "materiality_min_peer_group_size" in warning


def test_config_sane_is_none_when_at_least_one_group_reaches_the_floor():
    # 4 symbols in the same asset_class -> 3 peers each -- meets floor of 3
    universe = {"AAPL": "US_EQUITY", "MSFT": "US_EQUITY", "GOOG": "US_EQUITY",
               "AMZN": "US_EQUITY"}
    assert assert_materiality_config_sane(
        materiality_w5=1.0, symbol_universe=universe, min_peer_group_size=3) is None


def test_config_sane_is_none_when_a_smaller_asset_class_group_still_meets_a_lower_floor():
    universe = {"AAPL": "US_EQUITY", "MSFT": "US_EQUITY"}
    assert assert_materiality_config_sane(
        materiality_w5=1.0, symbol_universe=universe, min_peer_group_size=1) is None
