"""T3 materiality screen (§3.2): the deterministic cost firewall.

T3 is pure local arithmetic -- no model call, ever. This file tests the
screen in isolation, against synthetic candidates, with no collector (T1/T2)
and no calibration harness present -- both are explicitly out of scope for
this unit. `eligible_universe` and `cooldown_symbols` are passed in as plain
sets, and `capability_policy` is a real `agent.policy.TradeCapabilityPolicy`
(reused, not reimplemented) -- there is no in-cooldown or eligible-universe
tracker anywhere in the codebase yet, so the screen treats both as externally
supplied facts, exactly like `analyses_today`/`approvals_today` already are.
"""
import math
import sys
from datetime import datetime, timezone

import pytest

from agent.materiality import (MaterialityCandidate, MaterialityInputError,
                               MaterialityPolicy, compute_score, filing_weight,
                               screen)
from agent.policy import initial_policy

T0 = datetime(2026, 7, 20, 15, 0, tzinfo=timezone.utc)
CAPS = initial_policy()

POLICY = MaterialityPolicy(version="mat-v1", w1=1.0, w2=1.0, w3=1.0, w4=1.0,
                           w5=1.0, w6=1.0, threshold=2.0)


def candidate(**over):
    kw = dict(symbol="ACME", asset_class="US_EQUITY", ret_since_open=0.0,
              atr_20=1.0, volume_so_far=1.0, median_volume_same_time=1.0,
              sector_ret=0.0, earnings_proximity=0.0, form_type=None,
              item_codes=())
    kw.update(over)
    return MaterialityCandidate(**kw)


def run(cand, **over):
    kw = dict(policy=POLICY, capability_policy=CAPS, live=True,
              analyses_today=0, max_model_analyses_per_day=8,
              approvals_today=0, max_approval_requests_per_day=4,
              eligible_universe=frozenset({"ACME"}), cooldown_symbols=frozenset(),
              event_id="e1", event_type="FILING", source_id="edgar:1",
              observed_at=T0, effective_at=T0)
    kw.update(over)
    return screen(cand, **kw)


# ----------------------------------------------------------- filing weights

@pytest.mark.parametrize("item", ["2.02", "4.02", "1.01", "5.02", "7.01"])
def test_material_8k_items_carry_weight(item):
    assert filing_weight("8-K", (item,)) == 1.0


@pytest.mark.parametrize("item", ["9.01", "3.01", "8.01", "1.02"])
def test_non_material_8k_items_carry_no_weight(item):
    assert filing_weight("8-K", (item,)) == 0.0


def test_8k_with_no_items_carries_no_weight():
    assert filing_weight("8-K", ()) == 0.0


def test_8k_mixing_material_and_immaterial_items_still_carries_weight():
    assert filing_weight("8-K", ("9.01", "2.02")) == 1.0


@pytest.mark.parametrize("form", ["10-K", "10-Q", "10-k", "10-q"])
def test_annual_and_quarterly_reports_carry_weight(form):
    assert filing_weight(form) == 1.0


@pytest.mark.parametrize("form", ["3", "4", "5", "SC 13G", "SC 13D", "8-A12B", "S-8"])
def test_routine_ownership_and_administrative_forms_carry_no_weight(form):
    assert filing_weight(form) == 0.0


def test_no_filing_at_all_carries_no_weight():
    assert filing_weight(None) == 0.0


def test_unknown_form_type_defaults_to_no_weight():
    """Allowlist, not a heuristic: anything not explicitly named is zero,
    including a plausible-looking form nobody has listed here."""
    assert filing_weight("6-K") == 0.0


# --------------------------------------------------------------- score math

def test_compute_score_matches_hand_calculation():
    cand = candidate(ret_since_open=0.5, atr_20=0.25, volume_so_far=300.0,
                     median_volume_same_time=100.0, sector_ret=0.1,
                     earnings_proximity=0.4, form_type="8-K", item_codes=("2.02",))
    score, components = compute_score(cand, POLICY, analyses_today=2,
                                      max_model_analyses_per_day=8)
    import math
    term1 = abs(0.5) / 0.25                    # 2.0
    term2 = math.log(300.0 / 100.0)             # log(3)
    term3 = 1.0                                 # material 8-K
    term4 = 0.4
    term5 = abs(0.5 - 0.1) / 0.25               # 1.6
    term6 = 2 / 8                               # 0.25
    expected = (1.0 * term1 + 1.0 * term2 + 1.0 * term3 + 1.0 * term4
               + 1.0 * term5 - 1.0 * term6)
    assert score == pytest.approx(expected)
    assert components["score"] == pytest.approx(expected)


def test_budget_brake_reduces_score_as_analyses_accumulate():
    cand = candidate(ret_since_open=1.0, atr_20=1.0)
    low, _ = compute_score(cand, POLICY, analyses_today=0, max_model_analyses_per_day=8)
    high, _ = compute_score(cand, POLICY, analyses_today=7, max_model_analyses_per_day=8)
    assert high < low


def test_zero_volume_so_far_does_not_crash():
    cand = candidate(volume_so_far=0.0, median_volume_same_time=100.0)
    score, components = compute_score(cand, POLICY, analyses_today=0,
                                      max_model_analyses_per_day=8)
    assert math.isfinite(components["weighted_terms"]["volume"])


def test_zero_volume_orders_strictly_below_any_legitimate_sub_median_ratio():
    """Regression test for the bug where volume_ratio == 0 produced
    term2 == 0.0 == log(1) -- i.e. 'trading exactly at median volume', the
    opposite of what a zero reading means. This is an ordering test, not an
    equality test on a magic value: the bug was that the special case
    landed in the wrong place on the scale, not that it had the wrong
    literal number, and only an ordering check catches that class of bug."""
    zero_score, zero_components = compute_score(
        candidate(volume_so_far=0.0, median_volume_same_time=100.0),
        POLICY, analyses_today=0, max_model_analyses_per_day=8,
    )
    zero_term2 = zero_components["weighted_terms"]["volume"]

    for ratio in (0.5, 0.1, 0.01, 0.001):
        below_score, below_components = compute_score(
            candidate(volume_so_far=ratio * 100.0, median_volume_same_time=100.0),
            POLICY, analyses_today=0, max_model_analyses_per_day=8,
        )
        below_term2 = below_components["weighted_terms"]["volume"]
        assert zero_term2 < below_term2, (
            f"zero-volume term2 ({zero_term2}) must be strictly below the "
            f"term2 for ratio={ratio} ({below_term2}); zero volume is not "
            "parity with the median"
        )


def test_nonpositive_atr_is_refused_not_guessed():
    cand = candidate(atr_20=0.0)
    with pytest.raises(MaterialityInputError):
        compute_score(cand, POLICY, analyses_today=0, max_model_analyses_per_day=8)


def test_nonpositive_median_volume_is_refused_not_guessed():
    cand = candidate(median_volume_same_time=0.0)
    with pytest.raises(MaterialityInputError):
        compute_score(cand, POLICY, analyses_today=0, max_model_analyses_per_day=8)


def test_negative_volume_so_far_is_refused():
    cand = candidate(volume_so_far=-1.0)
    with pytest.raises(MaterialityInputError):
        compute_score(cand, POLICY, analyses_today=0, max_model_analyses_per_day=8)


def test_nonpositive_analysis_budget_is_refused():
    cand = candidate()
    with pytest.raises(MaterialityInputError):
        compute_score(cand, POLICY, analyses_today=0, max_model_analyses_per_day=0)


def test_only_risk_profile_style_change_changing_weights_changes_the_score():
    """Mirrors the risk-profile invariant: changing only the policy must
    actually change observable behaviour."""
    cand = candidate(ret_since_open=1.0, atr_20=1.0)
    a, _ = compute_score(cand, POLICY, analyses_today=0, max_model_analyses_per_day=8)
    other = MaterialityPolicy(version="mat-v2", w1=5.0, w2=1.0, w3=1.0, w4=1.0,
                              w5=1.0, w6=1.0, threshold=2.0)
    b, _ = compute_score(cand, other, analyses_today=0, max_model_analyses_per_day=8)
    assert a != b


# ------------------------------------------------------- score_components

def test_score_components_reconstruct_the_score():
    cand = candidate(ret_since_open=0.3, atr_20=0.2, sector_ret=0.05)
    score, components = compute_score(cand, POLICY, analyses_today=1,
                                      max_model_analyses_per_day=8)
    assert sum(components["weighted_terms"].values()) == pytest.approx(score)
    assert components["weights"] == {"w1": 1.0, "w2": 1.0, "w3": 1.0,
                                     "w4": 1.0, "w5": 1.0, "w6": 1.0}
    assert components["threshold"] == 2.0
    assert "raw_terms" in components


def test_screen_result_carries_gates_in_score_components():
    o = run(candidate(ret_since_open=5.0, atr_20=0.1))
    assert "gates" in o.score_components
    assert set(o.score_components["gates"]) == {
        "meets_threshold", "in_eligible_universe", "capability_allowed",
        "not_in_cooldown", "approvals_under_cap",
    }


# --------------------------------------------------------- trigger conjunction

def test_all_conditions_met_triggers_and_is_not_suppressed():
    o = run(candidate(ret_since_open=5.0, atr_20=0.1))   # big score, clears 2.0
    assert o.analysis_status == "PENDING_ANALYSIS"
    assert o.suppressed_reason is None
    assert o.symbols == ("ACME",)
    assert o.threshold_version == "mat-v1"


def test_below_threshold_is_not_material_and_not_suppressed():
    o = run(candidate(ret_since_open=0.0, atr_20=1.0))   # score well under 2.0
    assert o.analysis_status == "NOT_MATERIAL"
    assert o.suppressed_reason is None


def test_below_threshold_is_not_suppressed_even_if_another_gate_would_also_fail():
    """suppressed_reason means 'materiality cleared the bar but something
    else stopped it' -- a sub-threshold event that also happens to be outside
    the universe is just not material, not suppressed."""
    o = run(candidate(ret_since_open=0.0, atr_20=1.0, symbol="ZZZZ"),
           eligible_universe=frozenset({"ACME"}))
    assert o.analysis_status == "NOT_MATERIAL"
    assert o.suppressed_reason is None


def test_above_threshold_outside_eligible_universe_is_suppressed():
    o = run(candidate(ret_since_open=5.0, atr_20=0.1, symbol="ZZZZ"),
           eligible_universe=frozenset({"ACME"}))
    assert o.analysis_status == "SUPPRESSED"
    assert o.suppressed_reason == "not_in_eligible_universe"


def test_above_threshold_capability_denied_is_suppressed():
    """Reuses the real TradeCapabilityPolicy -- CRYPTO is DISABLED in
    Appendix E, so this proves real reuse rather than a stub that always
    says yes."""
    o = run(candidate(ret_since_open=5.0, atr_20=0.1, symbol="BTCUSD",
                      asset_class="CRYPTO"),
           eligible_universe=frozenset({"BTCUSD"}))
    assert o.analysis_status == "SUPPRESSED"
    assert o.suppressed_reason == "capability_denied"


def test_above_threshold_in_cooldown_is_suppressed():
    o = run(candidate(ret_since_open=5.0, atr_20=0.1),
           cooldown_symbols=frozenset({"ACME"}))
    assert o.analysis_status == "SUPPRESSED"
    assert o.suppressed_reason == "in_cooldown"


def test_above_threshold_at_approval_cap_is_suppressed():
    o = run(candidate(ret_since_open=5.0, atr_20=0.1),
           approvals_today=4, max_approval_requests_per_day=4)
    assert o.analysis_status == "SUPPRESSED"
    assert o.suppressed_reason == "approval_cap_reached"


def test_above_threshold_failing_multiple_gates_lists_all_reasons():
    o = run(candidate(ret_since_open=5.0, atr_20=0.1, symbol="ZZZZ"),
           eligible_universe=frozenset({"ACME"}),
           cooldown_symbols=frozenset({"ZZZZ"}),
           approvals_today=4, max_approval_requests_per_day=4)
    assert o.analysis_status == "SUPPRESSED"
    reasons = set(o.suppressed_reason.split(","))
    assert reasons == {"not_in_eligible_universe", "in_cooldown", "approval_cap_reached"}


def test_a_suppressed_event_is_a_real_returned_record_not_dropped():
    o = run(candidate(ret_since_open=5.0, atr_20=0.1, symbol="ZZZZ"),
           eligible_universe=frozenset({"ACME"}))
    assert o is not None
    assert o.materiality_score >= POLICY.threshold


# ---------------------------------------------------- the cost-firewall test

def test_screen_makes_zero_model_calls():
    """The entire reason T3 exists (§3.2): pure local arithmetic, no model
    call, ever. No model-client abstraction is built yet in this codebase
    (T2/T4 are out of scope for this unit), so this plants a poison double
    at every plausible model-client import location and runs every branch of
    screen() -- triggered, each of the four suppression reasons, below
    threshold, zero and material filing weight, and the budget brake --
    while it's in place, asserting the poison is never touched. If
    agent.materiality (directly or via anything it imports) ever reaches for
    a model client, the poison raises immediately and this test fails loudly.
    When a real model client lands with T4, extend this to patch that
    concrete class directly as well."""

    class Poison:
        def __getattr__(self, name):
            raise AssertionError(
                f"T3 materiality screen touched model-client attribute {name!r} "
                "-- T3 must make zero model calls, ever (§3.2)"
            )

        def __call__(self, *a, **kw):
            raise AssertionError(
                "T3 materiality screen invoked something callable on the "
                "model client -- T3 must make zero model calls, ever (§3.2)"
            )

    poisoned = {"anthropic": Poison(), "agent.llm": Poison(), "agent.model": Poison()}
    saved = {k: sys.modules.get(k) for k in poisoned}
    sys.modules.update(poisoned)
    try:
        scenarios = [
            dict(),  # triggers
            dict(candidate_over=dict(ret_since_open=0.0, atr_20=1.0)),  # not material
            dict(candidate_over=dict(symbol="ZZZZ"),
                run_over=dict(eligible_universe=frozenset({"ACME"}))),
            dict(candidate_over=dict(symbol="BTCUSD", asset_class="CRYPTO"),
                run_over=dict(eligible_universe=frozenset({"BTCUSD"}))),
            dict(run_over=dict(cooldown_symbols=frozenset({"ACME"}))),
            dict(run_over=dict(approvals_today=4, max_approval_requests_per_day=4)),
            dict(candidate_over=dict(form_type="8-K", item_codes=("2.02",))),
            dict(candidate_over=dict(form_type="3")),  # zero filing weight
            dict(run_over=dict(analyses_today=7, max_model_analyses_per_day=8)),
        ]
        for scenario in scenarios:
            cand_kwargs = dict(ret_since_open=5.0, atr_20=0.1)
            cand_kwargs.update(scenario.get("candidate_over", {}))
            cand = candidate(**cand_kwargs)
            run(cand, **scenario.get("run_over", {}))

        with pytest.raises(MaterialityInputError):
            compute_score(candidate(atr_20=0.0), POLICY, analyses_today=0,
                         max_model_analyses_per_day=8)
        # Reaching here at all is the assertion: Poison.__getattr__/__call__
        # raise immediately on any touch, so every screen()/compute_score()
        # call above would have already failed the test if either module had
        # been imported and used.
    finally:
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v
