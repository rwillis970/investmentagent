# Phase 2/3 Live-Acceptance Readiness

Follow-up unit to the overnight mission (`dd961e5`). Closes the remaining
Phase 2/3 acceptance gaps and delivers one safe, out-of-session command
(`--research-once`) Ray can run this weekend to collect real facts and
perform real materiality screening, with no trading path reachable from it
at all.

## 1. Starting / ending HEAD

- Starting HEAD: `dd961e5` (`main`) — "Overnight: Phase 1 recovery
  provenance, Phase 2 dashboard truth, Phase 3 opportunity-event
  persistence, reboot/backup readiness"
- Ending HEAD (code): `c12598b` (`main`) — "Phase 2/3 live-acceptance
  follow-up: durable materiality wiring + --research-once"
- This report itself is committed as a follow-up commit on top of
  `c12598b`.

## 2. Files changed

```
agent/dashboard_server.py                        |  23 ++
agent/dashboard_state.py                         | 124 ++++++-
agent/edgar_collector.py                         |  18 +-
agent/news_collector.py                          |  12 +-
agent/pipeline_stage.py                          |  43 ++-
agent/research_once.py                           | 328 +++++++++++++++++  (new)
scripts/phase_acceptance.py                      | 430 +++++++++++++++-----
scripts/run_agent.py                             | 181 ++++++++++
scripts/run_dashboard.py                         |  50 +++
scripts/runtime_health.py                        |  85 +++--
tests/test_dashboard_server.py                   |  88 +++++
tests/test_dashboard_state.py                    |  15 +-
tests/test_dashboard_state_opportunity_events.py | 158 ++++++++  (new)
tests/test_phase_acceptance.py                   | 267 +++++++++---
tests/test_pipeline_stage.py                     |  86 +++++
tests/test_research_once.py                      | 442 +++++++++++++++++++++  (new)
tests/test_run_agent.py                          | 236 ++++++++++++
tests/test_run_dashboard.py                      |   5 +
tests/test_runtime_health.py                     |  40 +-
19 files changed, 2436 insertions(+), 195 deletions(-)
```

(`agent/dashboard_server.py`, `agent/dashboard_state.py`,
`scripts/run_dashboard.py` and their three test files are Task 1's
dashboard-materiality wiring, completed and tested in the session
immediately prior to this window and committed together with the rest of
this unit's own work in `c12598b`, not re-done here.)

## 3. Dashboard materiality source of truth

`agent/dashboard_state.py` reads `agent.opportunity_event_store.
OpportunityEventStore`'s real `materiality_events.jsonl` directly. Session
and day counts are derived from each event's own persisted
`analysis_status` and its store-recorded `evaluated_at`, bucketed against
real session/day boundaries (`agent.market_calendar`) — never a T4-outcome
proxy, and never a fabricated fallback value when the store is empty or
missing (an empty/missing store reports a genuine zero/NOT_YET_OBSERVED
state, not a guessed number).

## 4. Phase 2 acceptance source of truth

`scripts/phase_acceptance.py`'s `_phase2_criteria` reads `agent.store.
FactStore`'s real `facts.jsonl`. Four criteria: at least one real,
externally-collected `Fact` exists; every fact has non-empty provenance
(`source_id`); every fact's `observed_at`/`effective_at` are valid,
timezone-aware instants with `observed_at >= effective_at` or a
documented, per-collector exception; a second, independently-constructed
`FactStore` over the same path reloads to the same fact set. Missing or
empty `facts.jsonl` reports `NOT_YET_OBSERVED` for all four, never `FAIL`
and never silently promoted to `PASS`.

## 5. Phase 3 acceptance source of truth

`scripts/phase_acceptance.py`'s `_phase3_criteria` (rewritten this unit,
Task 2) reads `agent.opportunity_event_store.OpportunityEventStore`'s real
`materiality_events.jsonl` — replacing the prior session's
`OpportunityEventTracker` T4-outcome-only proxy entirely. Five criteria:
the event references a real persisted `Fact` (FILING events match a
`field="filing"` fact by `source_id`/`entity_id`/`observed_at`; PRICE_MOVE
events match any `market_snapshot` fact for one of the event's symbols at
or before its own `observed_at`); the event's `event_id` reconstructs
deterministically from `f"{source_id}:{symbol}:{observed_at.isoformat()}"`;
`materiality_score`/`threshold_version` are both present and finite;
`analysis_status` is one of `PENDING_ANALYSIS`/`SUPPRESSED`/`NOT_MATERIAL`;
a second, independently-constructed store over the same path reloads to
the same event (same score/status/threshold_version). A `SUPPRESSED` or
`NOT_MATERIAL` event alone satisfies all five — a `PENDING_ANALYSIS`
trigger is explicitly not required (T4 analysis remains a separate Phase 4
criterion, untouched by this unit).

## 6. runtime_health materiality source

`scripts/runtime_health.py`'s `_last_materiality_evaluation` (rewritten
this unit) reads the same `OpportunityEventStore`, reporting
`most_recent_evaluated_at` (the max of every event's own store-recorded
`evaluated_at`), `total_events`, and a `by_status` breakdown
(`PENDING_ANALYSIS`/`SUPPRESSED`/`NOT_MATERIAL` counts) — `PASS` only when
at least one real event exists, `NOT_YET_OBSERVED` for a missing or empty
store, `UNAVAILABLE` only on a genuine read failure (e.g. a corrupt file).

## 7. `--research-once` architecture

New `agent/research_once.py` + `scripts/run_agent.py --research-once`.
Refuses immediately (before touching any store) unless the persisted mode
is exactly `PAUSED`. On success: collects EDGAR filings and news
unconditionally (they have no session gate of their own), collects market
data only when `--account-id`/`--key-id`/`--secret-ref` are all three
supplied and the market session allows a truthful read, persists every new
fact through the real `FactStore`, runs one `agent.materiality_cycle.
run_materiality_cycle` pass over the freshly-persisted facts, persists
every resulting `OpportunityEvent` through the real `OpportunityEventStore`,
and reports counts. `ModeStore.write` is never called anywhere in this
module (static AST proof: `tests/test_research_once.py::
test_mode_store_write_is_never_called_structurally`) — the persisted mode
is read once, at the top, and never touched again. No `agent.ledger.
Ledger` is ever constructed (a disclosed simplification: every candidate
screens with `held_symbols=frozenset()`/`cooldown_symbols=frozenset()`,
i.e. always `side="BUY"`, never cooldown-suppressed — see the module's own
`held_and_cooldown_awareness` field on the result, always populated with
this explanation).

## 8. Exact collectors invoked

1. `agent.edgar_collector.collect_filings` — SEC EDGAR 8-K/10-K/10-Q
   filings for every symbol in `cfg.symbol_universe`.
2. `agent.news_collector.collect_news_events` — via `cfg.news_feed_provider`
   (`NullNewsProvider` by default in both `config.example.json` and Ray's
   real `config.json` — collects nothing, but is still "attempted" and
   reports `COLLECTED` with 0 facts, not skipped).
3. `agent.market_data_collector.collect_market_data` — only if
   `--account-id`/`--key-id`/`--secret-ref` are all three given.

Then one `agent.materiality_cycle.run_materiality_cycle` pass over
whatever was just persisted.

## 9. Closed-market behavior by collector

- **Market data**: `NOT_YET_OBSERVED` (with an explicit reason) if no
  credentials were given, if `now` falls on a non-trading day, or if `now`
  is before today's own session open. If the market provider can
  truthfully retrieve the most recent completed bar, it is collected —
  this is not refused merely for being `--research-once`.
- **EDGAR filings**: always attempted, any hour, any day (no session gate
  of its own). A transport/network failure is caught and reported
  `NOT_YET_OBSERVED`, never a crash.
- **News**: always attempted, any hour, any day, same fail-safe posture as
  EDGAR.

`collected_now` (`now` passed into the command) is always reported
separately from each fact's own `effective_at`/`observed_at` — no future
leakage: a fact observed after `now` is never screened this cycle (proven
in `tests/test_research_once.py::
test_no_future_leakage_a_fact_observed_after_now_is_never_screened`).

## 10. Proof no order path is reachable

- Static AST proof (`test_module_never_imports_pipeline_or_approval_or_
  model_machinery`, `test_module_never_calls_submit_or_cancel` in
  `tests/test_research_once.py`): `agent/research_once.py` imports no
  `Gatekeeper`, `StagedOrder`, approval-execution, or T4/model machinery,
  and calls no `.submit(`/`.cancel(` anywhere.
- CLI-level static proof (`test_research_once_statically_cannot_reach_an_
  order_or_approval_machinery` in `tests/test_run_agent.py`): the actual
  source of `scripts/run_agent.py::_run_research_once` is grepped (not
  merely inferred from test behavior) for
  `execute_approved_request`/`approval_execution`/`Gatekeeper`/
  `PipelineRuntime`/`pipeline_stage`/`build_pipeline_runtime`/
  `.submit(`/`.cancel(`/`mint_approval_token`/`pipeline=`/
  `AlpacaPaperAdapter`/`AlpacaLiveAdapter` — none present; `_run_research_
  once` has no `pipeline` parameter at all.
- Repo-wide static scan (this unit): exactly **one** production
  `adapter.submit(` call site in the entire codebase —
  `agent/approval_execution.py:451` — and it is unreachable from
  `--research-once` by the proof above.

## 11. Proof PAUSED remains PAUSED

- `test_refuses_when_persisted_mode_is_not_paused` /
  `test_refuses_for_every_non_paused_mode` (parametrized over
  DISABLED/RESEARCH/PAPER/PRODUCTION_ACTIVE) / `test_a_refusal_touches_no_
  store_at_all`: any non-`PAUSED` persisted mode refuses before touching
  any collaborator.
- `test_mode_store_write_is_never_called_structurally`: static AST proof,
  `ModeStore.write` is never called anywhere in the module.
- CLI-level: `test_research_once_runs_with_no_credentials_and_leaves_mode_
  paused` (mode is `PAUSED` before and after a successful run) and
  `test_research_once_refuses_when_mode_is_not_paused` (mode is `PAPER`
  before and after a refused run — untouched either way).

## 12. Durable-before-T4 invariant

`agent/pipeline_stage.py::run_pipeline_stage` now tracks
`persisted_event_ids` from every `opportunity_event_store.record()` call
that completes without raising (a fresh write and a legitimate duplicate
no-op both count as "durably persisted"; an `OpportunityEventStoreError`
does not). The `triggered` list that feeds T4 analysis now additionally
requires `_durably_persisted(e)`. Four new tests in `tests/
test_pipeline_stage.py` prove: persistence success lets an eligible event
reach T4; persistence failure keeps that one event out of the T4 candidate
list; a persistence failure on one event does not block unrelated events
in the same cycle; the pre-existing, separately-disclosed
"`opportunity_event_store` not wired at all" case is unaffected (out of
this task's scope by the mission's own wording — "a persistence failure
may allow the overall collector loop to continue").

## 13. Python / JS test counts

- Baseline at the start of this window: 5089 Python, 56 JS.
- Current, on a disposable copy (`/tmp/investmentagent_test_copy`, fresh
  `cp -a` from the real repo at `c12598b`): **5145 Python passed**, **56
  JS passed**. (+56 Python vs. baseline: +20 from Task 1/2's own tests in
  the prior session, +4 from Task 4, +25 from `test_research_once.py`
  direct unit tests, +7 from the new `--research-once` CLI tests in
  `test_run_agent.py`.)
- No regressions anywhere in the full suite.

## 14. Static safety scan

- **adapter.submit call sites**: exactly one in production code —
  `agent/approval_execution.py:451`. Every other match in `agent/pipeline.
  py`, `agent/approval_bridge.py`, `agent/approval_execution.py`,
  `scripts/run_dashboard.py`, `scripts/phase_acceptance.py`,
  `scripts/preflight_broker.py` is prose/docstring/comment, not a call.
- **T4/Claude disabled**: `t4_analysis_enabled: bool = False` in `agent/
  config.py`'s dataclass default, `"t4_analysis_enabled": false` in
  `config.example.json`, and absent (so defaulted `False`) from Ray's real
  `config.json`.
- **Materiality threshold**: `"materiality_threshold": 2.0` in both
  `config.example.json` and Ray's real `config.json`, unchanged.
- **PAUSED immutability through research-once**: proven in §11 above,
  both statically and via CLI-level before/after assertions.
- **Canonical data untouched**: `data/` is not git-tracked (`git ls-files
  data/` → 0 files) and every file currently in the real `data/` directory
  has an mtime of 2026-08-14 or earlier — nothing in this session (2026-08-
  15) wrote to it. All test runs used disposable copies at
  `/tmp/investmentagent_test_copy`, never the real repo or real `data/`.

## 15. Exact live command Ray should run

Real repo root: `/Users/raywillis/projects/investmentagent`. Run from a
Terminal in that directory.

**Simplest, no Alpaca credentials (EDGAR + news + screening only — this is
the recommended first run):**

```
python3 scripts/run_agent.py \
  --config config.json \
  --data-dir data \
  --research-once
```

**With market data too** (only if you want a real, in-session/closed-
market-truthful price snapshot as well — substitute your real Alpaca
paper key-id and the same `--secret-ref` your LaunchAgent plist already
uses):

```
python3 scripts/run_agent.py \
  --config config.json \
  --data-dir data \
  --account-id PA3XZX944LRR \
  --key-id <your Alpaca paper key id> \
  --secret-ref <your Keychain secret-ref> \
  --research-once
```

Both forms require the persisted mode to already be `PAUSED` (it currently
is — confirmed via `data/mode_state.jsonl`) and will refuse if the
scheduled loop (or another one-shot writer) is running against the same
`--data-dir` at the time.

## 16. Expected output

A single `INFO`-level log line summarizing the whole run, plus one extra
line per collector that has a `reason` worth surfacing (e.g. market data
skipped for no credentials), for example:

```
--research-once complete: persisted_mode=PAUSED market_data=NOT_YET_OBSERVED(collected=0 dedup=0) edgar_filings=COLLECTED(collected=N dedup=M) news=COLLECTED(collected=0 dedup=0) materiality_evaluations=K triggered=0 suppressed=S not_material=T events_persisted=K events_persistence_failed=0
market_data reason: no market data client configured (--key-id/--secret-ref not supplied)
```

Exit code `0` on success, `1` on any refusal or failure (never raises).

## 17. Exact evidence-inspection commands afterward

```
python3 scripts/inspect_evidence.py --data-dir data facts list
python3 scripts/inspect_evidence.py --data-dir data facts list --entity-id AAPL
python3 scripts/inspect_evidence.py --data-dir data opportunities list
python3 scripts/inspect_evidence.py --data-dir data opportunities list --status SUPPRESSED
```

Read-only; never mutates `data/`.

## 18. Exact Phase 2/3 acceptance command

```
python3 scripts/phase_acceptance.py --account-id PA3XZX944LRR --mode PAPER --data-dir data
```

Read-only. Run just now (before any `--research-once` run against real
data), the Phase 2/Phase 3 opportunity-event criteria all report
`NOT_YET_OBSERVED` — expected and correct, since `data/facts.jsonl` and
`data/materiality_events.jsonl` do not exist yet. This is exactly what
running `--research-once` (§15) resolves; it is not a defect in this
unit's own code.

(The `alpaca_credentials_present`/`reconciles_*` criteria also report
`UNAVAILABLE` here because no `--key-id`/`--secret-ref` were given to this
read-only inspection — that family of criteria is unrelated to Phase 2/3
and unaffected by this unit.)

## 19. Exact pre-reboot commands

Reviewed `scripts/reboot_check.py` and `scripts/backup_snapshot.py`
read-only this unit (Task 5) — both are structurally read-only of source
(see each script's own module docstring); `reboot_check.py --mode pre`
writes only its own `--out` manifest file, `backup_snapshot.py` writes
only new, non-destructive timestamped archives under `--backup-dir`.

**Before rebooting:**

```
python3 scripts/backup_snapshot.py \
  --data-dir data --backup-dir backups

python3 scripts/reboot_check.py --mode pre \
  --data-dir data --backup-dir backups \
  --out backups/pre_reboot_manifest.json
```

**Reboot the Mac.**

**After rebooting**, from the same repo directory:

```
python3 scripts/reboot_check.py --mode post \
  --data-dir data --backup-dir backups \
  --prior backups/pre_reboot_manifest.json \
  --out backups/post_reboot_result.json

python3 scripts/runtime_health.py --data-dir data --config config.json
```

`reboot_check.py --mode post` exits non-zero if anything append-only
shrank, was rewritten, or the persisted mode changed unexpectedly — read
its printed report (or `backups/post_reboot_result.json`) before doing
anything else. Note the currently-active `failure_sentinel` (`TypeError`,
first seen 2026-08-13, 386 consecutive occurrences, never recovered) —
unrelated to this unit and untouched by `--research-once` (which never
reads or writes `agent.failure_sentinel` at all); it will still be there
after the reboot unless separately cleared via `--reconcile-once` or a
real scheduled-loop cycle, neither of which this report is instructing
you to run.

## 20. Deployment / restart requirement

**None.** `--research-once` is a flag on the already-deployed `scripts/
run_agent.py` entry point — no new file needs installing, no LaunchAgent
needs editing, and per the mission's own explicit instruction, no
recurring job (cron/launchd) has been installed for it. Run it manually,
as many times as you like, whenever you want to collect research this
weekend; the process lock (shared with the scheduled loop) prevents it
from ever racing a real cycle.

---

## PHASE 2/3 LIVE ACCEPTANCE READY: YES

The acceptance harness (`scripts/phase_acceptance.py`,
`scripts/runtime_health.py`) now reads exclusively from the real,
durable `FactStore`/`OpportunityEventStore` for every Phase 2/Phase 3
criterion — no T4-outcome proxy, no fabricated fallback. `--research-once`
is built, tested (25 direct unit tests + 7 CLI integration tests, all
passing), and structurally incapable of reaching an order, an approval, a
broker adapter, or T4/Claude — proven both statically (AST/source-grep) and
behaviorally. The durable-before-T4 invariant is closed and tested. The
persisted mode is provably unreachable for mutation from this command. The
full suite (5145 Python + 56 JS) passes with no regressions, and the
repo-wide static scan confirms the single production order call site,
`t4_analysis_enabled=false`, and `materiality_threshold=2.0` are all
unchanged. The only thing currently missing is real evidence on disk — a
fact of Ray's account never having run this command yet, not a gap in this
unit's own work — and running the command in §15 this weekend is exactly
what resolves it.
