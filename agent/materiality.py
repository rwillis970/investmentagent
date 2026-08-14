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
still have no tracker INSIDE this module (`screen()` takes both as plain
caller-supplied sets, exactly the way it already takes `analyses_today` and
`approvals_today` as given counts rather than computing them from a live
counter store). UPDATE (§2, §11 Day 4 collectors unit, Commit 4): real
suppliers for both now exist elsewhere -- `agent.config.Config.
symbol_universe` (a `{SYMBOL: asset_class}` allowlist) supplies
`eligible_universe`, and `agent.holding.symbols_in_cooldown` (walking
`Ledger.lots()`'s closed lots against each lot's own frozen
`cooldown_period`) supplies `cooldown_symbols`. `agent.materiality_cycle.
run_materiality_cycle` is the glue that builds real candidates and calls
`screen()` with both -- this module itself still takes them as plain
arguments, by design, so it stays testable against synthetic candidates
with neither supplier present.

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

# REVIEW FIX (Commit 5, §2/§11 Day 4 collectors unit): §3.2 writes this term
# as "filing_weight[form_type, item_codes]" -- a TABLE, not the flat `1.0`
# `filing_weight` used to return for every allowlisted form/item, which
# meant `w3` could only scale every material filing together, never
# distinguish (say) a restatement (4.02) from a Reg FD disclosure (7.01).
# `DEFAULT_FILING_WEIGHTS` reproduces the OLD flat-1.0 behaviour exactly --
# every key `MATERIAL_8K_ITEMS`/`WEIGHTED_FORMS` already allowlisted, each
# at 1.0 -- so every existing caller that never passes its own `weights`
# (below) is unaffected. The REAL, differentiated numbers are an
# uncalibrated placeholder same as materiality_w1-w6: see `agent.config.
# Config.materiality_filing_weights` and its own comment for where a real
# config would override this table, and why it is not calibrated in this
# commit either.
DEFAULT_FILING_WEIGHTS: dict[str, float] = {
    **{f"8-K:{item}": 1.0 for item in sorted(MATERIAL_8K_ITEMS)},
    **{form: 1.0 for form in sorted(WEIGHTED_FORMS)},
}


class MaterialityInputError(ValueError):
    """A candidate's inputs cannot produce a reliable score (non-positive
    ATR, non-positive median volume, negative volume, a non-positive
    analysis budget, or -- REVIEW FIX, see compute_score's own comment on
    the UNKNOWN-INPUT RULE -- a term whose raw value is `None` while its
    policy weight is nonzero). §3.2's score is pure arithmetic over trusted
    upstream inputs; when they're malformed OR UNKNOWN UNDER A LIVE WEIGHT,
    the fail-safe-to-no-trade invariant means refusing to score rather than
    substituting a guessed contribution that could silently manufacture or
    mask a signal."""


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
    # REVIEW FIX (Commit 5): the per-form/item table `filing_weight` scores
    # against -- see that function and `DEFAULT_FILING_WEIGHTS` above.
    # Required, no default, same "no implicit fallback" posture as every
    # other field on this policy: a caller must say explicitly what table a
    # score was computed against, never silently inherit one.
    filing_weights: dict[str, float]


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
    # `None` when no reliable value could be derived (e.g. `agent.
    # materiality_cycle`'s peer-median substitute below its own configured
    # minimum peer-group size) -- distinct from a real, computed value of
    # exactly `0.0`. See compute_score's UNKNOWN-INPUT RULE comment for how
    # `None` here is handled, uniformly with `earnings_proximity` below.
    sector_ret: float | None
    earnings_proximity: float | None
    form_type: str | None = None
    item_codes: tuple[str, ...] = ()


def filing_weight(form_type: str | None, item_codes: tuple[str, ...] = (), *,
                  weights: dict[str, float] | None = None) -> float:
    """§3.2 writes this as `filing_weight[form_type, item_codes]` -- a TABLE,
    not a flat scalar (REVIEW FIX, Commit 5: this used to return a flat
    `1.0` for every allowlisted form/item, so `w3` could only scale every
    material filing together, never distinguish one item from another).

    `weights` IS the allowlist now, not merely a scaling on top of one: a
    form or item absent from `weights` is zero, the same default-deny
    posture as every other allowlist in this codebase -- dropping a key
    entirely is a legitimate way to express "this no longer carries any
    weight". Defaults to `DEFAULT_FILING_WEIGHTS` (module-level, reproduces
    the exact old flat-1.0 allowlist) so a direct call with no `weights`
    behaves exactly as before this fix; production callers (`agent.
    materiality_cycle`, via `compute_score` below) always pass a policy's
    own `MaterialityPolicy.filing_weights` explicitly.

    An 8-K matching MORE THAN ONE weighted item takes the MAXIMUM weight
    among the matching items, not their sum -- this is one §3.2 term, and a
    filing reporting several routine items alongside one material one must
    not score higher than the material item alone would."""
    if weights is None:
        weights = DEFAULT_FILING_WEIGHTS
    if not form_type:
        return 0.0
    form = form_type.upper()
    if form == "8-K":
        item_weights = [weights.get(f"8-K:{code}", 0.0) for code in item_codes]
        return max(item_weights) if item_weights else 0.0
    return weights.get(form, 0.0)


def compute_score(candidate: MaterialityCandidate, policy: MaterialityPolicy, *,
                  analyses_today: int, max_model_analyses_per_day: int
                  ) -> tuple[float, dict]:
    """The six-term score from §3.2, plus a `score_components` dict complete
    enough to reconstruct the decision after the fact: the weights used, the
    raw per-candidate inputs, each term's weighted contribution, and the
    threshold it was compared against. `screen()` adds a `gates` key on top
    of this for the trigger conjunction's own four conditions.
    """
    # NON-FINITE INPUT GUARD (Unit D, reconstructed 2026-08-13). NaN/
    # Infinity are NOT caught by the plain comparisons below: every
    # comparison against NaN is False in Python/IEEE754, so `float("nan")
    # <= 0` is False and a NaN atr_20 would otherwise slip straight past
    # the very next check, divide ret_since_open by NaN, and produce a NaN
    # `score` -- which then compares False against `score >= threshold`
    # for ANY threshold, silently reporting "not material" for what is
    # actually corrupted/invalid market data, with no error raised and
    # nothing logged anywhere. These four fields have no "unknown is
    # legitimate" case (unlike earnings_proximity/sector_ret below) --
    # they are always required, so they are always validated, unlike the
    # weight-gated checks further down.
    for _field_name, _value in (
        ("ret_since_open", candidate.ret_since_open),
        ("atr_20", candidate.atr_20),
        ("volume_so_far", candidate.volume_so_far),
        ("median_volume_same_time", candidate.median_volume_same_time),
    ):
        if not math.isfinite(_value):
            raise MaterialityInputError(
                f"{candidate.symbol}: {_field_name} must be finite, got {_value!r}"
            )
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

    # UNKNOWN-INPUT RULE (REVIEW FIX): `earnings_proximity` and `sector_ret`
    # can each be `None` -- "we don't know", not "we know and it's zero" --
    # for reasons neither term controls (no forward earnings calendar exists;
    # a symbol's declared peer group is too small -- see agent/earnings.py
    # and agent/materiality_cycle.py respectively). An unknown input under a
    # ZERO weight is harmless -- the term is already structurally inert, so
    # "unknown" and "known-and-irrelevant" are observationally the same to
    # the score -- but an unknown input under a NONZERO weight is exactly
    # the "malformed input" case this function already refuses to guess
    # through for atr_20/median_volume_same_time/volume_so_far above: a
    # weight that is live but multiplied by a fabricated stand-in could
    # silently manufacture or mask a signal, and the fail-safe-to-no-trade
    # invariant means refusing to score rather than doing that. This is
    # DISQUALIFICATION, not renormalisation: the candidate is refused
    # entirely (raised here, caught and skipped per-symbol by `agent.
    # materiality_cycle.run_materiality_cycle`, exactly like a non-positive
    # atr_20 already is) rather than recomputed over a smaller set of known
    # terms -- renormalising would let two candidates with different amounts
    # of missing data get compared against the SAME threshold on
    # DIFFERENT bases, which would undermine the Day-11 calibration
    # harness's single global threshold (§3.2). Both terms are held to this
    # SAME rule -- there is no reason for "unknown proximity" and "unknown
    # peer return" to be handled differently, and before this fix they
    # were not (earnings_proximity silently contributed zero regardless of
    # w4; this was the inconsistency flagged for this fix).
    if policy.w4 != 0 and candidate.earnings_proximity is None:
        raise MaterialityInputError(
            f"{candidate.symbol}: earnings_proximity is unknown but "
            f"materiality_w4={policy.w4!r} is nonzero; refusing to score "
            "rather than treat an unknown input as zero under a live weight (§3.2)"
        )
    if policy.w5 != 0 and candidate.sector_ret is None:
        raise MaterialityInputError(
            f"{candidate.symbol}: sector_ret is unknown but "
            f"materiality_w5={policy.w5!r} is nonzero; refusing to score "
            "rather than treat an unknown input as zero under a live weight (§3.2)"
        )
    # Same non-finite guard as the four unconditional fields above, but
    # weight-gated: a non-finite value under a LIVE weight is the same
    # "malformed input, refuse to score" case; under a ZERO weight it is
    # never even inspected (see term4_for_score/sector_ret_for_score below,
    # which substitute 0.0 whenever the weight is zero, unconditionally --
    # NOT merely when the raw value happens to be None -- 0.0 * nan is
    # itself NaN in IEEE754, so relying on the weight multiplication alone
    # to zero out a non-finite raw term would not actually work).
    if (policy.w4 != 0 and candidate.earnings_proximity is not None
            and not math.isfinite(candidate.earnings_proximity)):
        raise MaterialityInputError(
            f"{candidate.symbol}: earnings_proximity must be finite, got "
            f"{candidate.earnings_proximity!r}"
        )
    if (policy.w5 != 0 and candidate.sector_ret is not None
            and not math.isfinite(candidate.sector_ret)):
        raise MaterialityInputError(
            f"{candidate.symbol}: sector_ret must be finite, got {candidate.sector_ret!r}"
        )

    term1_momentum = abs(candidate.ret_since_open) / candidate.atr_20
    volume_ratio = candidate.volume_so_far / candidate.median_volume_same_time
    term2_volume = math.log(max(volume_ratio, _VOLUME_RATIO_FLOOR))
    fw = filing_weight(candidate.form_type, candidate.item_codes,
                      weights=policy.filing_weights)
    term3_filing = fw
    # Below this point a `None` can only survive under a ZERO weight (the
    # guard above already raised otherwise), so substituting 0.0 for the
    # raw arithmetic is safe -- the substitution can never actually move
    # the score, only avoid a TypeError from subtracting/multiplying
    # `None`. The RAW value (which may still be `None`) is preserved in
    # `raw_terms` below, unmodified, so a human auditing `score_components`
    # later can tell "unknown" apart from "known, and exactly zero".
    term4_earnings = candidate.earnings_proximity
    # 0.0 whenever the weight is zero, unconditionally -- not merely when
    # the raw value is None -- see the non-finite guard comment above for
    # why (0.0 * nan is itself nan; substitution must happen before the
    # multiply, not be assumed to happen because of it).
    term4_for_score = (0.0 if policy.w4 == 0
                       else (term4_earnings if term4_earnings is not None else 0.0))
    sector_ret_for_score = (0.0 if policy.w5 == 0
                            else (candidate.sector_ret if candidate.sector_ret is not None
                                  else 0.0))
    term5_idiosyncratic = abs(candidate.ret_since_open - sector_ret_for_score) / candidate.atr_20
    term6_budget_brake = analyses_today / max_model_analyses_per_day

    weighted_terms = {
        "momentum_vs_atr": policy.w1 * term1_momentum,
        "volume": policy.w2 * term2_volume,
        "filing": policy.w3 * term3_filing,
        "earnings_proximity": policy.w4 * term4_for_score,
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
          observed_at, effective_at, side: str = "BUY"):
    """The full §3.2 trigger conjunction over one candidate, always returning
    an `OpportunityEvent` -- triggered, not-material, or suppressed. A
    suppressed event (materiality cleared the bar but another condition
    didn't) is a real, persisted record with `suppressed_reason` set, never
    a dropped one. No model call happens anywhere on this path -- see
    `tests/test_materiality.py::test_screen_makes_zero_model_calls`.

    `side` (REVIEW FIX, Commit 5): the capability check used to hardcode
    `side="BUY"` unconditionally, so a material event on a symbol this
    account already HOLDS -- where the warranted action is an exit, i.e. a
    SELL -- would be wrongly evaluated against BUY's own capability status
    rather than SELL's. Defaults to `"BUY"`, preserving every existing
    caller that only ever screens candidates for symbols not currently
    held; a caller that knows a symbol is held should pass `side="SELL"`
    (see `agent.materiality_cycle.run_materiality_cycle`'s `held_symbols`
    parameter, added the same commit, for where that determination is
    made).
    """
    score, components = compute_score(
        candidate, policy, analyses_today=analyses_today,
        max_model_analyses_per_day=max_model_analyses_per_day,
    )

    meets_threshold = score >= policy.threshold
    in_universe = candidate.symbol in eligible_universe
    capability_ok = capability_policy.allows(
        gate=Gate.UNIVERSE, live=live, symbol=candidate.symbol,
        asset_class=candidate.asset_class, side=side, funding="SETTLED_CASH",
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
