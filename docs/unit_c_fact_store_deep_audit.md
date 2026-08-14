# Unit C: Phase 2 fact-store deep audit (reconstructed)

Status: RECONSTRUCTED FROM CURRENT SOURCE, independently tested. No prior
Unit C findings from the lost overnight session were recoverable from the
transcript in enough concrete detail to reproduce as stated (the lost
summary referenced "Phase 2 deep audit" only at the level of a checklist
item, with no specific defect described) -- this entire unit is therefore
classified as REPORTED-BUT-LOST -> re-derived from scratch, and every
finding below is either a new finding or an independently confirmed piece
of existing behaviour, never a blind copy of anything from the lost
transcript.

Scope: trace the Phase 2 mechanism (§2/§11 Day 4: `agent.store.FactStore`,
`agent.market_data_collector`, `agent.edgar_collector`) end to end --
provider -> normalization -> provenance -> durable fact -> dedup ->
retrieval -> downstream consumer -- reading actual source and tests, not
assuming behaviour from module docstrings alone.

## 1. FactStore crashes entirely on a crash-truncated trailing line — NEW FINDING, fixed and tested

**Classification: new finding.**

### Root cause

`agent/store.py::FactStore._load()` read every line of `facts.jsonl` and
called `json.loads(line)` with no exception handling at all. A
crash-truncated last line (SIGKILL, power loss, or disk full mid-write --
all realistic: Unit B's own reconstruction proved SIGKILL is a normal,
expected event this system must tolerate) raised `json.JSONDecodeError`
uncaught, which meant the ENTIRE store failed to construct -- not just
that one row. Reproduced directly against current source: a two-line
`facts.jsonl` with the second line hand-truncated mid-JSON raised on
`FactStore(path)`, discarding visibility into the first (intact) row too.

Practical consequence: every subsequent process restart (the dashboard,
`scripts/run_agent.py`, `phase_acceptance.py`, anything that constructs a
`FactStore` against that data directory) would crash identically, forever,
until an operator manually edited the file by hand -- a crash LOOP, not a
one-time data-loss event. This is exactly the failure class task #326
("Get runtime out of crash loop") already fixed for a different
mechanism; `agent/store.py` had never received the equivalent treatment.

This also directly contradicts the codebase's own already-stated
philosophy for this exact file. `agent/mode_store.py::ModeStore.write`'s
own docstring explains FactStore deliberately does NOT fsync, because
"losing the last few unflushed rows on an unclean shutdown is a
completeness gap, not a safety one" for this specific store. If losing a
row is an accepted, tolerated gap, a crash landing on that row must not
ALSO take every earlier, perfectly intact row down with it -- that turns
an accepted small gap into a total outage.

### Fix

`agent/store.py::FactStore._load()`, mirroring the established pattern
`agent.audit.AuditLog._load` already uses for the identical problem in a
different file (this codebase's own precedent, not invented fresh):

- A malformed **last** line is tolerated: recorded verbatim on the new
  `FactStore.truncated_tail_on_load` attribute (same name/shape as
  `AuditLog.truncated_tail_on_load`), logged as a warning, and loading
  continues with every row before it.
- A malformed line that is **not** the last line still raises
  `StoreError`. Unlike `AuditLog`, `FactStore` has no fsync-based write
  ordering guarantee to appeal to (that guarantee is what lets
  `AuditLog` confidently call a non-final malformed row "tampering, not a
  crash") -- `FactStore` cannot make that same positive claim. Given
  that, silently skipping a malformed row from the middle of an
  append-only evidence store would be a worse failure mode (quiet data
  loss with no trace) than refusing to load, so it stays conservative and
  raises rather than guessing.

`fsync` was deliberately NOT added to `FactStore.append` as part of this
fix -- that would reverse an existing, explicitly reasoned decision in
`agent/mode_store.py`'s own docstring, not close a gap this unit was
asked to investigate. Flagged as a real, disclosed limit below rather
than silently changed.

### A second-order interaction found and fixed in the same pass

`scripts/phase_acceptance.py::_phase2_criteria` (Unit F, this same
reconstruction session, committed `57c4e90`) constructs a `FactStore`
against the live data directory and previously treated ANY exception at
construction as `UNAVAILABLE`. Once the toleration fix above landed, a
file whose only content is unparseable (the exact fixture
`tests/test_phase_acceptance.py::test_a_corrupt_fact_store_file_is_
unavailable_never_a_silent_pass` uses) no longer raises at all -- it now
loads successfully as an empty store with `truncated_tail_on_load` set,
which would have silently reported `NOT_YET_OBSERVED` ("fact store file
exists but is empty") instead of the correct `UNAVAILABLE` ("we do not
know -- something was written but is unreadable"), a real regression the
existing Unit F test caught immediately on the first full-suite run after
this fix. Corrected `_phase2_criteria` to check
`store.truncated_tail_on_load` explicitly and report `UNAVAILABLE` when
set, before falling through to the existing n>0/n==0 PASS/NOT_YET_OBSERVED
logic -- preserving the intended honesty distinction ("no facts yet" vs.
"cannot tell") across both units rather than letting one unit's fix
silently break the other's test.

### Tests — independently reproduced now

2 new tests in `tests/test_store_as_of.py`:
`test_a_crash_truncated_trailing_line_is_tolerated_not_fatal` (RED before
the fix: raised `json.JSONDecodeError`; GREEN after: loads the intact
first row, sets `truncated_tail_on_load` to the raw truncated text) and
`test_a_malformed_line_that_is_not_the_last_line_still_raises` (confirms
the conservative middle-of-file behaviour is deliberate and tested, not
accidental).

## 2. Provider -> normalization -> durable fact -> dedup -> retrieval — previously reported, independently confirmed

**Classification: previously reported and independently confirmed** (the
lost summary's checklist named this trace; the actual chain was
re-verified against current source, not assumed).

- **`agent/market_data_collector.py`** (T1, `SOURCE_ID = "alpaca_market_
  data"`, `FIELD = "market_snapshot"`): fetches via
  `AlpacaMarketDataClient`, computes `atr_20`/`ret_since_open`/
  `volume_so_far`/`median_volume_same_time` in memory, writes ONE bundled
  `Fact` per symbol per cycle with `observed_at=effective_at=now`. No
  explicit dedup logic exists here, and none is needed: a market snapshot
  is fresh, legitimately-different data every cycle (not a re-poll of a
  fixed historical record the way a filing is) -- each cycle's `Fact` is
  a genuinely new observation, so append-without-checking is correct, not
  a gap. Fail-safe-per-symbol (skip-and-record, not raise-and-lose-every-
  other-symbol) confirmed via source reading; 15 existing tests in
  `tests/test_market_data_collector.py` cover the ATR/same-time-metrics
  arithmetic and the skip paths.
- **`agent/edgar_collector.py`** (T2, `SOURCE_ID = "sec_edgar"`, `FIELD =
  "filing"`): dedup IS explicit and necessary here, because EDGAR
  re-reports the same filing on every future poll -- `collect_filings`
  reads `store.now_view().history(symbol, FIELD)`, builds a `known`
  set of `accession_number`s already on record, and skips any filing
  whose `accession_number` is already known, BEFORE writing. Confirmed
  both by reading the source and by running the existing
  `test_a_second_cycle_does_not_re_write_the_same_accession_number` test
  (`tests/test_edgar_collector.py`), which asserts `len(store) == 1` --
  "no duplicate row" -- after two identical collection cycles.
  `observed_at` uses EDGAR's own `acceptanceDateTime` when present, a
  deliberately-LATE (never early) fallback otherwise, so the store's own
  look-ahead invariant (`as_of(t)` never returns `observed_at > t`) is
  never violated in the direction that would matter (claiming knowledge
  of a filing before EDGAR actually made it public).
- **`agent/store.py::AsOfView`** (retrieval): `get_fact`/`get`/`history`
  all route through `bisect_right` against the per-`(entity_id, field)`
  sorted `observed_at` list, with an explicit belt-and-braces assertion
  (`if fact.observed_at > self._t: raise StoreError(...)`) — the
  look-ahead invariant is enforced twice, not merely documented once.
  Existing coverage: `test_as_of_cannot_see_the_future`,
  `test_before_first_observation_returns_none`,
  `test_restatement_is_a_new_row_and_does_not_rewrite_history`,
  `test_out_of_order_append_is_ordered_correctly` (a late-arriving fact
  with an EARLIER `observed_at` is still ordered correctly relative to
  facts already in the store) -- all read and confirmed still passing.
- **Downstream consumer** (`agent/materiality_cycle.py::build_materiality_
  candidates`): reads `market_snapshot`/`filing` facts back out via
  `view.get`/`view.history`, confirmed in Unit D's own reconstruction
  this session (`docs/unit_d_materiality_deep_audit.md`) to feed
  deterministic, replay-safe `OpportunityEvent`s with no cross-cycle
  hidden state.
- **Cadence/restart safety** (`agent/pipeline_stage.py`'s own module
  docstring, read directly): explicitly reasons that a process restart
  losing its in-memory `last_collected_at`/`last_screened_at` watermarks
  is "safe, not a money risk", because EDGAR's own accession-number dedup
  and the store's single-threaded, synchronous loop already make
  re-collection after a restart a safe no-op/fresh-fact rather than a
  duplicate. Read and confirmed consistent with the dedup mechanisms
  actually present in both collector modules above.

No gaps were found in this area; no new tests were added here beyond
confirming the existing ones still pass.

## Test results

- `tests/test_store_as_of.py`: 11 passed (9 pre-existing + 2 new).
- `tests/test_phase_acceptance.py`: 14 passed (12 pre-existing + the
  regression this unit's own fix introduced and then corrected --
  net count unchanged from Unit F's own commit, behavior changed).
- `tests/test_market_data_collector.py`: 15 passed (no changes; read
  and confirmed as existing coverage).
- `tests/test_edgar_collector.py`: 24 passed (no changes; read and
  confirmed as existing coverage, including the accession-number dedup
  test specifically).
- Full project suite (`pytest -q`): **4876 passed**.

## Disclosed scope limits

- `agent/mode_store.py::ModeStore._load` has the SAME unguarded
  `json.loads()` pattern `FactStore._load` had before this fix -- a
  crash-truncated trailing row in `mode_state.jsonl` would crash
  `ModeStore` construction identically, and `ModeStore` is on the
  startup-critical path (mode/PAUSED-state persistence, Unit E). This
  was found while reading `agent/mode_store.py` for the fsync-precedent
  comment cited above, but fixing it is OUTSIDE Phase 2 fact-store scope
  -- disclosed here as a related finding for a future unit, not fixed in
  this one. (Note: `ModeStore.write` DOES fsync, unlike `FactStore.
  append`, so the "last line is the only possible crash victim" reasoning
  would actually apply more cleanly there than it does to `FactStore`
  itself -- a `ModeStore`-specific fix could plausibly mirror
  `AuditLog._load`'s pattern even more directly.)
- `fsync` was deliberately not added to `FactStore.append` -- see the
  fix section above. This preserves the existing, explicitly-reasoned
  design decision in `agent/mode_store.py`'s docstring rather than
  silently reversing it.
- Raw daily/minute bars are never persisted by `agent.market_data_
  collector` (only the derived four-number snapshot) -- confirmed as a
  documented, deliberate scope decision in that module's own docstring
  (storage-cost tradeoff, no consumer needs bar-level history back out),
  not investigated further as a gap since the module's own reasoning is
  sound and this unit found no consumer that contradicts it.
