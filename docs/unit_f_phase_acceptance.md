# Unit F — phase_acceptance.py review/rebuild (reconstructed 2026-08-13)

STATUS OF PRIOR REPORT: a previous session reported reviewing (not
building) `scripts/phase_acceptance.py`, implying it already existed.
Independently checked against the CURRENT real repo: `scripts/
phase_acceptance.py` **does not exist** (`wc -l scripts/phase_acceptance.py`
→ "No such file or directory"; `tests/test_phase_acceptance.py` likewise
absent). This file was never in the real repo — it existed only inside the
lost `/tmp` worktree, alongside everything else described in that report.
Rebuilt fresh this session, incorporating the Phase 3 fix and the
diagnose_account read-only finding from the start (both independently
re-derived against current source, not copied from the lost report's
claims — see below for exactly how each was re-verified).

## Design

Read-only PASS/FAIL/UNAVAILABLE/NOT YET OBSERVED harness over three exit
criteria: Phase 1 (§8.1 Day 3 reconciliation, via `agent.diagnostics.
diagnose_account`, reused not reimplemented), Phase 2 (§2/§11 Day 4: at
least one fact durably recorded), Phase 3 (§3.1/§3.2: materiality
screening has produced at least one real, qualifying, analyzed
opportunity). Never imports `agent.pipeline`/`agent.approval*`/`agent.
pipeline_stage`, never calls `.submit()`/`.cancel()` — both proven by
static AST inspection in `tests/test_phase_acceptance.py`, not just
asserted in prose.

## `diagnose_account` read-only verification (independently re-derived)

Re-traced the CURRENT `agent/diagnostics.py` (not assumed from the lost
report): `grep -n "\.write(\|open(\|maybe_mark_recovered\|failure_sentinel\.\|
runtime_status\.\|\.save(\|fsync" ` restricted to `diagnose_account`'s own
body (lines 164–489 of this worktree's baseline commit) surfaces exactly
one hit — `failure_sentinel.load(sentinel_path)`, a READ. No write, no
`open()` in write mode, no `fsync`. The one write this module is allowed to
perform, `maybe_mark_recovered` (a SEPARATE function, starting at line
492), is never called from `diagnose_account` and is never referenced
anywhere in `scripts/phase_acceptance.py` — proven both by direct grep and
by a static AST test (`test_harness_never_calls_or_imports_maybe_mark_
recovered`) that checks actual `ast.Import`/`ast.Call` nodes, not a naive
text search (a naive `"maybe_mark_recovered" not in source` check would
have failed on this very doc-comment's own mention of the function by
name — caught and fixed during this unit). **Conclusion: `diagnose_account`
is genuinely read-only, no qualification needed** — same conclusion the
lost report reached, but re-derived independently against current source
rather than trusted from that report.

## Phase 3 defect (independently rediscovered)

Same finding as the lost report described, re-derived from scratch against
current source: `agent/opportunity_event_tracker.py`'s durable file does
NOT store raw materiality-screen triggers — it stores T4-analysis TERMINAL
OUTCOMES only (`"analyzed"`/`"refused"`/`"budget_exceeded"`/
`"insufficient_settled_cash"`, confirmed via `grep -n 'outcome="'
agent/pipeline_stage.py`), written via `mark_handled()`, reachable only
from `_analyze_and_request`, gated behind `pipeline.t4_analysis_enabled`
(default False). A bare "file exists, has non-empty lines" check both
false-positives (PASS on nothing but non-qualifying outcome rows) and
false-negatives (permanently NOT YET OBSERVED whenever T4 is disabled).
Built the fix in from the start this time (not built naive-then-fixed,
since the defect was already known before writing the first line): the
Phase 3 criterion requires at least one row with `outcome == "analyzed"`
specifically, parsed via `json.loads` per line.

**Correction versus the lost report's own (unverified) claim**: the real
default filename for this store, per `scripts/run_agent.py`'s own current
`_DEFAULT_STORE_FILENAMES` dict, is `opportunity_events.jsonl` — NOT
`opportunity_tracker.jsonl`, which is what the lost report's summary had
described. Verified directly (`grep -n "opportunity_tracker_path"
scripts/run_agent.py scripts/run_dashboard.py`) before writing a single
line of `phase_acceptance.py`, specifically because the instruction for
this unit was to trust current source over the lost report's claims. Using
the wrong filename would have made this criterion permanently read
NOT_YET_OBSERVED against a real, populated store — a real, if narrower,
version of the same false-negative class this unit exists to prevent.

**Correction versus the lost report's own (unverified) claim, #2**: Phase
1's reconciliation component names are `reconciliation_settled_cash`/
`reconciliation_positions`/`reconciliation_open_orders`/
`reconciliation_day_trades` (verified via `grep -n 'name=' agent/
diagnostics.py`) — not the shorter `settled_cash`/`positions`/
`open_orders`/`day_trade_count` the lost report's summary described. Using
the wrong names would have made every Phase 1 reconciliation criterion
permanently report UNAVAILABLE ("no component with this name") regardless
of the real underlying reconciliation state.

## Tests (14, all new, all passing)

Baseline coverage (8): no-credentials/no-data-dir → UNAVAILABLE/NOT YET
OBSERVED everywhere; NOT YET OBSERVED never promoted to PASS for an empty
fact store; a real fact on disk is a genuine PASS; a corrupt fact store is
UNAVAILABLE never a silent PASS; a missing secret is UNAVAILABLE not FAIL
and never raises; exit code is nonzero only on a real FAIL; the harness
never imports an execution path (AST); the harness never calls submit/
cancel (AST). Plus 1 new static test not in the lost report's own
description: the harness never calls or imports `maybe_mark_recovered`
(AST). Phase 3 (4): non-analyzed outcomes alone don't PASS; a real
"analyzed" row PASSes; a corrupt tracker file is UNAVAILABLE; an empty
tracker file is NOT YET OBSERVED not PASS. Phase 1 (1): every
reconciliation criterion is UNAVAILABLE with no adapter constructed (not
silently omitted, not fabricated).

Full suite after Unit F: 4863 Python tests passed (4849 baseline after
Unit E + 14 new), 34/34 JS tests passed.

## Answers to the specific questions asked

- **Genuinely read-only, no qualification needed** — see verification
  above.
- **No indirect write path**: confirmed — the only stores this script's
  functions ever construct (`FactStore`, and everything inside
  `diagnose_account`) are opened for reading in every code path this
  script exercises; no `LedgerStore`/`ModeStore`/`AuditLog` write method is
  ever called.
- **NOT YET OBSERVED never becomes PASS**: proven directly by
  `test_never_promotes_not_yet_observed_to_pass_for_an_empty_fact_store`
  and the three Phase 3 tests above.
- **Phase 1 cannot PASS while reconciliation fails**: each reconciliation
  criterion reads `diagnose_account`'s own component status directly
  (PASS/UNAVAILABLE/else→FAIL) — there is no code path that overrides a
  real FAIL from that component.
- **Phase 2 cannot PASS without durable real facts**: `len(FactStore) > 0`
  is the only PASS condition; an empty or missing file is NOT YET
  OBSERVED, a corrupt one is UNAVAILABLE.
- **Phase 3 cannot PASS without real qualifying opportunity evidence**:
  fixed this session — see Phase 3 defect section above.
- **Broker unavailable cannot accidentally PASS**: no credentials → no
  adapter → every reconciliation criterion reports UNAVAILABLE (proven by
  `test_phase1_reconciliation_criteria_are_unavailable_with_no_adapter`),
  never a fabricated PASS.
- **Persisted PAUSED is represented correctly**: out of scope for this
  specific script — `phase_acceptance.py` does not surface `ModeStore`'s
  `paused_from` at all (it only checks `diagnose_account`'s own
  `persisted_mode` component, which reports the current mode string, not
  the PAUSED/PAUSED-from distinction Unit E built for the dashboard). This
  is a real, disclosed scope boundary: Unit E and Unit F solve the same
  underlying "don't conflate broker environment with operational state"
  problem for two different surfaces (dashboard UI vs. this CLI harness)
  and were not unified in this session.

## What this unit did NOT do (disclosed)

- Did not close the "screen never fired vs. T4 disabled" ambiguity in
  Phase 3 — requires a genuine new OpportunityEvent store, out of scope.
- Did not surface `ModeStore.paused_from()` in this harness (see above).
