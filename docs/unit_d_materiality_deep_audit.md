# Unit D: Phase 3 materiality deep audit (reconstructed)

Status: RECONSTRUCTED FROM CURRENT SOURCE, independently tested. The
original Unit D work from the lost overnight session no longer exists on
disk (sandbox `/tmp` was recycled between sessions — see the top-level
reconstruction report for the full data-loss account). Nothing below is
copied from the lost transcript's conclusions; every claim in this
document was re-derived by reading `agent/materiality.py` and
`agent/materiality_cycle.py` as they exist in this worktree, and by
running real tests against them.

Scope covered: the six checklist items requested for Unit D — non-finite
input handling, determinism, deterministic event IDs, threshold-boundary
behaviour, duplicate/replay safety, and existing coverage for provenance /
stale-data / failure-to-no-opportunity behaviour.

## 1. NaN/Infinity input handling — NEW FINDING, fixed and tested

**Classification: new finding** (the lost report's summary claimed a "P0
non-finite-input fix" was already implemented; a `grep -n
'isfinite\|isnan\|isinf'` over `agent/materiality.py` before this unit's
work returned zero matches, so whatever the lost session did — if
anything — never reached this worktree's source. Treated as
REPORTED-BUT-LOST -> re-derived from scratch against current code, which
is why it is classified as a new finding rather than a confirmation.)

### Root cause

`compute_score()` in `agent/materiality.py` guards its inputs with plain
comparisons: `candidate.atr_20 <= 0`, `candidate.median_volume_same_time
<= 0`, `candidate.volume_so_far < 0`. Under IEEE754, every comparison
against `NaN` is `False` — `float("nan") <= 0` is `False`, `float("nan")
< 0` is `False`. A `NaN` value in any of these fields slips past every
one of these guards unchanged.

From there, `NaN` propagates arithmetically: `abs(ret_since_open) /
atr_20` becomes `NaN` if either operand is `NaN` or infinite; that
`NaN` flows into `score = sum(weighted_terms.values())`; and
`score >= policy.threshold` (in `screen()`) is `False` for a `NaN` score
regardless of what `threshold` is. The net effect: corrupted/invalid
market data (a `NaN` or `Infinity` snapshot value — plausible from a
malformed upstream feed, not merely a theoretical edge case) is silently
classified as "not material," with no exception raised and nothing
logged anywhere. This is a fail-safe violation in the wrong direction:
the project's invariant is fail-safe-to-NO-TRADE on uncertainty, but
silently scoring a corrupted input as "definitely not material" is not
refusing to act — it is acting confidently on bad data while looking
identical, from the outside, to a legitimate "nothing interesting
happened" cycle.

A second, related defect was found while writing the fix: the
pre-existing substitution pattern for `earnings_proximity`/`sector_ret`
(`term4_for_score = term4_earnings if term4_earnings is not None else
0.0`) does not neutralize a `NaN` raw value even under a zero weight,
because (a) the `None`-check does not catch `NaN` (a `NaN` is not
`None`), and (b) even if it did, `0.0 * float("nan")` is itself `NaN`,
not `0.0`, in IEEE754 — so relying on "the weight is zero, so the
product is zero" is not a valid assumption once the raw term can be
non-finite.

### Fix

`agent/materiality.py::compute_score`, two changes:

1. An unconditional finiteness guard (`math.isfinite`) over
   `ret_since_open`, `atr_20`, `volume_so_far`, `median_volume_same_time`
   — these four have no "unknown is legitimate" case, so they are always
   validated regardless of weights, raising `MaterialityInputError` on
   any non-finite value.
2. A weight-gated finiteness guard over `earnings_proximity`/`sector_ret`
   (only checked when `policy.w4`/`policy.w5` is nonzero, consistent with
   the existing `None`-under-nonzero-weight disqualification rule already
   in the function), plus redesigned substitution logic that sets
   `term4_for_score`/`sector_ret_for_score` to `0.0` **unconditionally
   whenever the corresponding weight is exactly zero** — not merely when
   the raw value is `None` — so a zero weight neutralizes the term by
   direct substitution before any multiplication happens, never by
   relying on `weight * value` to come out to zero.

Both raise `MaterialityInputError`, which `agent.materiality_cycle.
run_materiality_cycle` already catches per-symbol and records as a skip
reason (the same disqualification path a non-positive `atr_20` already
takes) — so a corrupted symbol is skipped-and-visible-in-`skipped`, not
silently absorbed into "no events this cycle."

### Tests — independently reproduced now

7 new RED->GREEN tests in `tests/test_materiality.py`:
`test_nan_atr_20_is_rejected_not_silently_scored`,
`test_infinite_ret_since_open_is_rejected_not_silently_scored`,
`test_nan_volume_so_far_is_rejected_not_silently_scored`,
`test_infinite_median_volume_same_time_is_rejected_not_silently_scored`,
`test_nan_earnings_proximity_is_rejected_when_w4_is_nonzero`,
`test_infinite_sector_ret_is_rejected_when_w5_is_nonzero`,
`test_nan_earnings_proximity_is_harmless_when_w4_is_zero`. All 7 failed
before the fix (confirming the defect was real and reproducible against
current code, not assumed) and passed after.

## 2. Threshold-boundary inclusivity — new finding (no prior coverage), verified

**Classification: new finding.** `screen()` compares `score >=
policy.threshold` (line 371) — an inclusive boundary: a score exactly
equal to the threshold triggers. No existing test in
`tests/test_materiality.py` exercised the exact-equality case before this
unit (existing coverage tested clearly-above and clearly-below values
only). Three new tests were added using a dedicated
`_SINGLE_TERM_POLICY` (all weights zero except `w1=1.0`, `threshold=2.0`,
so `score` is exactly controllable via `ret_since_open`/`atr_20`):
`test_score_exactly_equal_to_threshold_triggers_inclusive_boundary`,
`test_score_just_below_threshold_does_not_trigger`,
`test_score_just_above_threshold_triggers`. All pass, confirming the
inclusive-boundary behavior matches the `>=` in the source.

## 3. Determinism and deterministic event IDs — previously reported, independently confirmed

**Classification: previously reported and independently confirmed**
(the lost report's summary claimed determinism; this was re-verified
from current source rather than taken on faith).

`grep -n 'random\.\|datetime\.now\|uuid\.'` across both
`agent/materiality.py` and `agent/materiality_cycle.py` returns zero
matches. `event_id` is constructed in `agent/materiality_cycle.py` as
`f"{source_id}:{candidate.symbol}:{observed_at.isoformat()}"` — a pure
function of caller-supplied, already-deterministic inputs, with no
randomness, no UUID, and no wall-clock read anywhere in the module.

## 4. Duplicate/replay safety — new finding, new test, independently reproduced now

**Classification: new finding** (not explicitly covered by any existing
test before this unit, though implied by the determinism finding above).
Added `test_identical_inputs_replayed_twice_produce_byte_identical_events`
to `tests/test_materiality_cycle.py`: calls `run_materiality_cycle` twice
against the identical `FactStore` view and identical `now`, and asserts
field-for-field identity of the returned `events` (`event_id`, `symbols`,
`source_id`, `observed_at`, `score_components`, in order), plus identical
`skipped` and `degraded_reason`. This demonstrates the practical
consequence of the determinism finding above: a caller (e.g. an
`OpportunityEventTracker` keyed on `event_id`) can safely dedupe a
re-run after a crash/restart with a plain "have I seen this `event_id`
before" check — no special-cased replay logic is required at the
consumer layer, because the producer is already idempotent for identical
inputs. Test passes.

## 5. Existing coverage for provenance / stale-data / failure-to-no-opportunity — previously reported, independently confirmed, no new code needed

**Classification: previously reported and independently confirmed.**
Read (not merely grepped) the relevant existing tests in
`tests/test_materiality_cycle.py` rather than writing duplicate coverage:

- **Provenance**: `test_every_event_carries_a_data_provenance_note_for_sector_ret`
  confirms every event's `score_components["data_provenance"]["sector_ret"]`
  states plainly that `sector_ret` is a peer-median substitute, not a
  verified per-GICS-sector return — self-describing months later, per
  the test's own docstring.
- **Look-ahead guard (stale/future-data protection)**:
  `test_respects_the_look_ahead_guard_via_the_supplied_view` confirms the
  cycle only sees facts visible through the caller-supplied `store.
  now_view()`, not future-dated facts.
- **Failure-to-no-opportunity is visible, not silent**: `test_a_malformed_
  snapshot_is_skipped_not_raised`, together with the `degraded_reason`
  family of tests (`test_degraded_reason_is_set_when_every_symbol_fails_
  for_the_identical_reason`, `test_degraded_reason_is_none_when_skip_
  reasons_are_genuinely_mixed`, etc.), confirms a cycle that found nothing
  material because it never had a real chance to (every symbol skipped
  for the same reason, or an empty universe) is distinguishable from a
  cycle that genuinely ran clean and found nothing — via
  `MaterialityCycleResult.degraded_reason`, not silently identical
  output.

No gaps were found in this area; no new tests were added here.

## Test results

- `tests/test_materiality.py`: 67 passed (57 pre-existing + 7 NaN/
  Infinity + 3 threshold-boundary).
- `tests/test_materiality_cycle.py`: 34 passed (33 pre-existing + 1
  replay/determinism).
- Full project suite (`pytest -q`): **4874 passed**.

## Disclosed scope limits

- This unit did not re-derive or re-verify the calibration harness
  referenced in `agent/materiality.py`'s module docstring (Day-11
  calibration-against-replayed-history) — that is explicitly out of
  scope for `compute_score`/`screen` per the module's own docstring
  ("it does not calibrate the threshold against replayed history") and
  was not part of the Unit D checklist.
- `MaterialityInputError` disqualifies a symbol for the *current* cycle
  only; it does not implement any cross-cycle cooldown or alerting for a
  symbol that repeatedly produces non-finite data. That is a design
  choice inherited unchanged from the existing per-symbol skip mechanism,
  not something this unit added or evaluated for sufficiency.
