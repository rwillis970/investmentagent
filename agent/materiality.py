"""T3 materiality screen (§3.2): the deterministic cost firewall.

T3 sits between T2 Watch and T4 Analyse in the cadence loop (§3): only
deterministic local arithmetic runs here, and only a passing screen may
promote a candidate into a T4 model call. This module is exactly that
arithmetic and the trigger conjunction around it -- nothing else. It does
not collect market data, EDGAR filings or news (T1/T2, a separate unit), and
it does not calibrate the threshold against replayed history (the Day-11
calibration harness, also a separate unit). Both are out of scope here by
design: this module has to be fully testable against synthetic candidates
with neither one present.

WHAT THIS MODULE DOES NOT OWN. Two of the trigger conjunction's five
conditions -- `symbol in eligible_universe` and `not in_cooldown(symbol)` --
have no backing tracker anywhere in this codebase yet (verified before
writing this: `HoldingPolicy.cooldown_period` / `Config.trade_cooldown_period`
are stored durations nothing evaluates against a clock, and
`eligible_universe` names no structure at all). Rather than inventing
tracking state for either one under this unit, `screen()` takes both as
plain caller-supplied sets, exactly the way it already takes
`analyses_today` and `approvals_today` as given counts rather than computing
them from a live counter store. A real cooldown/universe tracker is a later,
separate unit's job.

WHAT THIS MODULE DOES OWN, AND REUSES RATHER THAN REIMPLEMENTS.
`capability_allows(symbol)` is backed by the real `agent.policy.
TradeCapabilityPolicy.allows()` -- the same object the risk constrainer and
the broker adapter check against, called the same minimal-dimension way
`risk.py`'s gate-2 check already does (only the dimensions that matter to a
pre-order screen are supplied; `TradeCapabilityPolicy.check` only validates
whichever dimensions it's given). No changes were needed to `agent/policy.py`
to support this.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from .entities import OpportunityEvent
from .policy import Gate, TradeCapabilityPolicy

# Floor for volume_so_far / median_volume_same_time before taking its log
# (term 2). volume_so_far == 0 is a legitimate reading -- the screen can run
# before the first print of the day -- so it is not treated as malformed
# input (unlike a non-positive ATR or median volume, which are data-quality
# errors, not real readings). But 0.0 is not a safe stand-in for the ratio
# itself: log(1) == 0.0, so an unclamped zero ratio would score as "trading
# exactly at its median volume", the opposite of what zero volume means, and
# would rank ABOVE every real sub-median reading rather than below all of
# them. Flooring the ratio at a small epsilon before the log keeps zero (and
# any vanishingly small nonzero ratio) below every legitimate sub-median
# reading, and keeps the term finite instead of -inf.
_VOLUME_RATIO_FLOOR = 1e-6

# §3.2: an explicit allowlist, not a heuristic. Only the 8-K items and form
# types named here carry weight; everything else -- routine ownership forms,
# administrative forms, and any form type this allowlist has never heard of
# -- carries none. Default deny, same direction as the capability gates.
MATERIAL_8K_ITEMS = frozenset({"2.02", "4.02", "1.01", "5.02", "7.01"})
WEIGHTED_FORMS = frozenset({"10-K", "10-Q"})


class MaterialityInputError(ValueError):
    """A candidate's inputs cannot produce a reliable score (non-positive
    ATR, non-positive median volume, negative volume, or a non-positive
    analysis budget). §3.2's score is pure arithmetic over trusted upstream
    inputs; when they're malformed, the fail-safe-to-no-trade invariant
    means refusing to score rather than substituting a guessed contribution
    that could silently manufacture or mask a signal."""


@dataclass(frozen=True)
class MaterialityPolicy:
    """w1-w6 and the threshold, versioned together as ONE unit under
    `version` (persisted on the resulting event as `threshold_version`).
    §3.2 is explicit that a threshold change is a policy version because it
    changes what the system trades -- and the weights a threshold was
    calibrated against change meaning together with it, so they share one
    version rather than drifting independently."""
    version: str
    w1: float
    w2: float
    w3: float
    w4: float
    w5: float
    w6: float
    threshold: float


@dataclass(frozen=True)
class MaterialityCandidate:
    """One candidate's raw inputs to the score function. Synthetic in tests;
    sourced from T1/T2 collectors in a later, separate unit."""
    symbol: str
    asset_class: str
    ret_since_open: float
    atr_20: float
    volume_so_far: float
    median_volume_same_time: float
    sector_ret: float
    earnings_proximity: float
    form_type: str | None = None
    item_codes: tuple[str, ...] = ()


def filing_weight(form_type: str | None, item_codes: tuple[str, ...] = ()) -> float:
    """§3.2: 8-K items 2.02, 4.02, 1.01, 5.02 and 7.01 carry weight; 10-K and
    10-Q carry weight; routine ownership and administrative forms carry
    none. An explicit allowlist -- an unlisted form or item is zero, never a
    guessed partial weight."""
    if not form_type:
        return 0.0
    form = form_type.upper()
    if form == "8-K":
        return 1.0 if any(code in MATERIAL_8K_ITEMS for code in item_codes) else 0.0
    if form in WEIGHTED_FORMS:
        return 1.0
    return 0.0


def compute_score(candidate: MaterialityCandidate, policy: MaterialityPolicy, *,
                  analyses_today: int, max_model_analyses_per_day: int
                  ) -> tuple[float, dict]:
    """The six-term score from §3.2, plus a `score_components` dict complete
    enough to reconstruct the decision after the fact: the weights used, the
    raw per-candidate inputs, each term's weighted contribution, and the
    threshold it was compared against. `screen()` adds a `gates` key on top
    of this for the trigger conjunction's own four conditions.
    """
    if candidate.atr_20 <= 0:
        raise MaterialityInputError(
            f"{candidate.symbol}: atr_20 must be positive, got {candidate.atr_20!r}"
        )
    if candidate.median_volume_same_time <= 0:
        raise MaterialityInputError(
            f"{candidate.symbol}: median_volume_same_time must be positive, "
            f"got {candidate.median_volume_same_time!r}"
        )
    if candidate.volume_so_far < 0:
        raise MaterialityInputError(
            f"{candidate.symbol}: volume_so_far cannot be negative, "
            f"got {candidate.volume_so_far!r}"
        )
    if max_model_analyses_per_day <= 0:
        raise MaterialityInputError(
            f"max_model_analyses_per_day must be positive, got "
            f"{max_model_analyses_per_day!r}"
        )

    term1_momentum = abs(candidate.ret_since_open) / candidate.atr_20
    volume_ratio = candidate.volume_so_far / candidate.median_volume_same_time
    term2_volume = math.log(max(volume_ratio, _VOLUME_RATIO_FLOOR))
    fw = filing_weight(candidate.form_type, candidate.item_codes)
    term3_filing = fw
    term4_earnings = candidate.earnings_proximity
    term5_idiosyncratic = abs(candidate.ret_since_open - candidate.sector_ret) / candidate.atr_20
    term6_budget_brake = analyses_today / max_model_analyses_per_day

    weighted_terms = {
        "momentum_vs_atr": policy.w1 * term1_momentum,
        "volume": policy.w2 * term2_volume,
        "filing": policy.w3 * term3_filing,
        "earnings_proximity": policy.w4 * term4_earnings,
        "idiosyncratic_vs_sector": policy.w5 * term5_idiosyncratic,
        "budget_brake": -policy.w6 * term6_budget_brake,
    }
    score = sum(weighted_terms.values())

    components = {
        "weights": {"w1": policy.w1, "w2": policy.w2, "w3": policy.w3,
                   "w4": policy.w4, "w5": policy.w5, "w6": policy.w6},
        "threshold": policy.threshold,
        "raw_terms": {
            "ret_since_open": candidate.ret_since_open,
            "atr_20": candidate.atr_20,
            "volume_so_far": candidate.volume_so_far,
            "median_volume_same_time": candidate.median_volume_same_time,
            "filing_weight": fw,
            "form_type": candidate.form_type,
            "item_codes": list(candidate.item_codes),
            "earnings_proximity": candidate.earnings_proximity,
            "sector_ret": candidate.sector_ret,
            "analyses_today": analyses_today,
            "max_model_analyses_per_day": max_model_analyses_per_day,
        },
        "weighted_terms": weighted_terms,
        "score": score,
    }
    return score, components


def screen(candidate: MaterialityCandidate, *, policy: MaterialityPolicy,
          capability_policy: TradeCapabilityPolicy, live: bool,
          analyses_today: int, max_model_analyses_per_day: int,
          approvals_today: int, max_approval_requests_per_day: int,
          eligible_universe: frozenset[str], cooldown_symbols: frozenset[str],
          event_id: str, event_type: str, source_id: str,
          observed_at, effective_at):
    """The full §3.2 trigger conjunction over one candidate, always returning
    an `OpportunityEvent` -- triggered, not-material, or suppressed. A
    suppressed event (materiality cleared the bar but another condition
    didn't) is a real, persisted record with `suppressed_reason` set, never
    a dropped one. No model call happens anywhere on this path -- see
    `tests/test_materiality.py::test_screen_makes_zero_model_calls`.
    """
    score, components = compute_score(
        candidate, policy, analyses_today=analyses_today,
        max_model_analyses_per_day=max_model_analyses_per_day,
    )

    meets_threshold = score >= policy.threshold
    in_universe = candidate.symbol in eligible_universe
    capability_ok = capability_policy.allows(
        gate=Gate.UNIVERSE, live=live, symbol=candidate.symbol,
        asset_class=candidate.asset_class, side="BUY", funding="SETTLED_CASH",
    )
    not_in_cooldown = candidate.symbol not in cooldown_symbols
    approvals_ok = approvals_today < max_approval_requests_per_day

    triggers = (meets_threshold and in_universe and capability_ok
               and not_in_cooldown and approvals_ok)

    components = dict(components)
    components["gates"] = {
        "meets_threshold": meets_threshold,
        "in_eligible_universe": in_universe,
        "capability_allowed": capability_ok,
        "not_in_cooldown": not_in_cooldown,
        "approvals_under_cap": approvals_ok,
    }

    suppressed_reason = None
    if meets_threshold and not triggers:
        reasons = []
        if not in_universe:
            reasons.append("not_in_eligible_universe")
        if not capability_ok:
            reasons.append("capability_denied")
        if not not_in_cooldown:
            reasons.append("in_cooldown")
        if not approvals_ok:
            reasons.append("approval_cap_reached")
        suppressed_reason = ",".join(reasons)

    if triggers:
        analysis_status = "PENDING_ANALYSIS"
    elif suppressed_reason:
        analysis_status = "SUPPRESSED"
    else:
        analysis_status = "NOT_MATERIAL"

    return OpportunityEvent(
        event_id=event_id, type=event_type, source_id=source_id,
        observed_at=observed_at, effective_at=effective_at,
        symbols=(candidate.symbol,), materiality_score=score,
        score_components=components, threshold_version=policy.version,
        analysis_status=analysis_status, suppressed_reason=suppressed_reason,
    )
