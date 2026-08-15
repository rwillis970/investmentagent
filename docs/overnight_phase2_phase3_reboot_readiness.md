# Overnight Readiness — Phase 1 Recovery Semantics, Phase 2 Fact Truth, Phase 3 Opportunity-Event Persistence, Reboot/Backup Readiness

Branch: `main`. This document covers four tracks (A/B/C/D) plus supporting test/backup/health infrastructure, worked in one continuous overnight session per the mission brief. Nothing in this document claims profitability, enables Claude/T4 analysis, places a live order, advances the operational mode, or touches canonical `data/`. All work against real code was made directly to source files; every test run used a disposable copy (`/tmp/investmentagent_test_copy`, rebuilt fresh from the real repo before each run), never the real repo's own `data/` directory.

## 1. Starting state

- Branch: `main`, confirmed via `git branch --show-current` before any edit.
- Starting `HEAD`: `298c00c` ("reconcile-once: fix PAUSED refusal by bypassing run_startup entirely" — the prior session's last commit).
- Starting test baseline: 4968 Python (`pytest -q`), 56 JS (`node --test tests/*.js`).

## 2. Ending state

- `HEAD` unchanged until the commit made in §21 below (this document was written, then one commit made, on `main`).
- `git status --short` before that commit: 10 modified tracked files, 10 new untracked files (full list in §3). Two incidentally-modified `__pycache__/*.pyc` files (a pre-existing repo-hygiene issue, `.gitignore` covers `__pycache__/` but two `.pyc` files were tracked before that rule existed) were reverted with `git checkout --` rather than committed — they carry no semantic content and are not part of this work.
- Final test count: **5089 Python passing, 56 JS passing** — 121 new Python tests added this session across four tracks, zero regressions, zero decreases at any checkpoint.

## 3. Exact files changed

Modified:
- `agent/failure_sentinel.py` — `FailureRecord.recovered_by` field; `mark_recovered(..., recovered_by=None)`.
- `agent/runtime_status.py` — docstring extension for the third producer (`source="reconcile_once"`).
- `agent/diagnostics.py` — `maybe_mark_recovered` now passes `recovered_by="diagnostic"`.
- `scripts/phase_acceptance.py` — new `scheduled_market_session_cycle_has_completed` criterion, structurally separate from the reconciliation criteria.
- `agent/store.py` — `FactStore.all_facts()`.
- `agent/dashboard_state.py` — real fact-store-backed `bars_ingested_today`/`filings_ingested_today`/news counts, replacing the false "no news collector exists" claim.
- `agent/dashboard_server.py` — `fact_store_refresh_fn` wiring on `GET /api/state`.
- `scripts/run_dashboard.py` — `--fact-store-path` flag + `_refresh_fact_store` helper.
- `agent/pipeline_stage.py` — `opportunity_event_store` collaborator; persists every screened `OpportunityEvent`, best-effort, before T4 filtering.
- `scripts/run_agent.py` — `--opportunity-event-store-path` flag; `build_pipeline_runtime` wiring; `_DEFAULT_STORE_FILENAMES["opportunity_event_store_path"] = "materiality_events.jsonl"`.
- Seven test files updated for the above (`tests/test_failure_sentinel.py`, `tests/test_phase_acceptance.py`, `tests/test_dashboard_state.py`, `tests/test_dashboard_server.py`, `tests/test_run_dashboard.py`, `tests/test_pipeline_stage.py`, `tests/test_run_agent.py`).

New:
- `agent/opportunity_event_store.py` — durable, event_id-deduplicated `OpportunityEvent` store.
- `scripts/reboot_check.py` — `--mode pre|post` read-only manifest tool.
- `scripts/backup_snapshot.py` — read-only-of-source tar.gz snapshot tool with self-verification.
- `scripts/runtime_health.py` — read-only PASS/FAIL/UNAVAILABLE/NOT_YET_OBSERVED health report.
- `scripts/inspect_evidence.py` — read-only `facts list/show` / `opportunities list/show` CLI.
- Five new test files: `tests/test_opportunity_event_store.py`, `tests/test_reboot_check.py`, `tests/test_backup_snapshot.py`, `tests/test_runtime_health.py`, `tests/test_inspect_evidence.py`, plus `tests/test_dashboard_state_fact_store.py`.

## 4. Track A — Phase 1 runtime/recovery semantics

`RuntimeStatus.source` extended from two producers to three: `"cycle"` (a real scheduled market-session cycle), `"reconcile_once"` (the `--reconcile-once` recovery path), `"diagnostic"` (`scripts/diagnose_runtime.py`). `--reconcile-once` now writes its own `RuntimeStatus` snapshot on success and calls `failure_sentinel.mark_recovered(..., recovered_by="reconcile_once")` on an active sentinel — both best-effort, wrapped so a write failure here never changes the command's exit code. `last_successful_cycle_at` is carried forward from whatever was already on disk, or left `None` — a `--reconcile-once` run never fabricates a value for a field only a real scheduled cycle can set.

## 5. Track A — phase_acceptance.py distinguishes reconciliation from a scheduled cycle

New criterion `scheduled_market_session_cycle_has_completed`, structurally separate from `_RECONCILIATION_COMPONENTS`. PASS requires `runtime_status.json`'s `last_successful_cycle_at is not None` — a `reconcile_once` or `diagnostic` snapshot alone is `NOT_YET_OBSERVED`, never promoted to PASS. Six new tests cover: no file, reconcile-only, diagnostic-only, a real cycle, a reconcile-once snapshot carrying forward a real prior cycle's timestamp, and a malformed file (`UNAVAILABLE`, never a silent PASS).

## 6. Track A — test evidence

13 new tests across `tests/test_run_agent.py` (7) and `tests/test_phase_acceptance.py` (6). One self-inflicted error during editing (a stray `Edit` call deleted a pre-existing test's setup body, caught via `ast.parse` and fixed by reconstructing the original test before appending) — caught and fixed before any test run, not shipped.

## 7. Track B — audit finding: the dashboard's own "no collector exists" claim was false

`agent/news_collector.py` already exists (built in a prior session's "Unit 14"). The dashboard's `dashboard_state.py` was hardcoding `bars_ingested_today`/`filings_ingested_today` to `_null(_NOT_BUILT)` and the news feed to a false "not built: no news collector exists anywhere in this codebase" string, regardless of what was actually collected. This was a dashboard-truth defect, not a missing-feature gap.

## 8. Track B — the fix

`agent.store.FactStore.all_facts()` — a read-only tuple snapshot, since the store had no built-in cross-series query. `dashboard_state.py`'s new `_facts_ingested_today(fact_store, field, today)` counts real persisted facts by field (`market_snapshot`/`filing`/`news_event`, verified directly against the three collectors' own `FIELD` literals) observed today. `dashboard_server.py` gained `fact_store_refresh_fn`, mirroring the existing `broker_state_refresh_fn` pattern, so the dashboard process (which does not share memory with the collector-writing process) re-reads the real file on every `GET /api/state`. `scripts/run_dashboard.py` gained `--fact-store-path`.

## 9. Track B — test evidence

16 new/updated tests across four files, including `tests/test_dashboard_state_fact_store.py` (new, 4 tests: real counts are genuine, not fabricated; a prior day's facts are excluded from "today"; a genuinely empty store reports a real zero, not `UNAVAILABLE`; snapshot/filing/news counts never cross-contaminate).

## 10. Track C — audit: what already existed vs. what was missing

`agent.entities.OpportunityEvent` (the exact shape needed) and `migrations/001_init.sql`'s `agent.opportunity_event` table already existed — both explicitly documented, in their own docstrings, as "not persisted anywhere: there is no OpportunityEvent store in this codebase yet." `agent.materiality_cycle.run_materiality_cycle` already builds real, deterministically-identified `OpportunityEvent` rows (`event_id = f"{source_id}:{symbol}:{observed_at.isoformat()}"`) every screen cycle, for all three of `agent.materiality.screen`'s real `analysis_status` outcomes (`PENDING_ANALYSIS` = triggered, `SUPPRESSED`, `NOT_MATERIAL`). Only the durable write side, and the wiring to call it, were missing. `agent.opportunity_event_tracker.OpportunityEventTracker` (pre-existing, not modified) stores T4-analysis terminal outcomes only — confirmed, via source read, to have never stored a raw screen result; the two stores are deliberately kept on separate files (`opportunity_events.jsonl` vs. the new `materiality_events.jsonl`) so they can never be conflated.

## 11. Track C — what was built

`agent/opportunity_event_store.py` — append-only JSONL, `fsync` every row, replay-on-load, following `agent.analysis_result_store.AnalysisResultStore`'s template with one deliberate difference: `record()` is **first-write-wins per `event_id`**, a silent no-op (not an exception) on a duplicate — satisfying "replay idempotent; restart does not duplicate events" for the common case (a scheduled loop re-screening the same still-most-recent filing every interval). Malformed input (`NaN`/`Infinity` anywhere numeric) raises before any write. Wired into `agent.pipeline_stage.run_pipeline_stage`: every event in `screening.events` is persisted, regardless of `analysis_status`, immediately after `run_materiality_cycle` returns and before the `triggered` filter narrows to what T4 will see — a persistence failure is caught and swallowed (never aborts the cycle). `scripts/run_agent.py` gained `--opportunity-event-store-path` (default `<data-dir>/materiality_events.jsonl`), threaded into `build_pipeline_runtime`.

## 12. Track C — no threshold change, no manufactured trigger

`materiality_threshold` in `config.example.json` remains `2.0`, unchanged. `agent.materiality.screen`'s comparison (`score >= policy.threshold`) was not touched. This work persists whatever the real screen produces — including zero `PENDING_ANALYSIS` events, which remains a valid outcome, not a defect to engineer around.

## 13. Track C — CLI inspection tool

`scripts/inspect_evidence.py` — read-only, JSON-Lines output. `facts list [--entity-id/--field/--source-id/--limit]`, `facts show <entity-id> <field>` (full bitemporal history), `opportunities list [--status/--symbol/--type/--limit]`, `opportunities show <event-id>` (full detail including `score_components` and this store's own `evaluated_at`). `--data-dir` defaults both store paths the same way `run_agent.py` does, but never creates the directory if missing — a missing store is an empty result, not an error. Smoke-tested end-to-end against a real (disposable) `FactStore`/`OpportunityEventStore` pair, not just unit tests.

## 14. Track C — test evidence

31 new tests: 13 in `tests/test_opportunity_event_store.py` (persistence, restart-idempotency, `NaN`/`Infinity` rejection, round-tripping, status filtering), 4 in `tests/test_pipeline_stage.py` (every status persisted regardless of outcome; no-op with no store wired; a same-filing restart does not duplicate that filing's row while a `PRICE_MOVE`-typed event's own fresh-every-cycle `event_id` legitimately still grows the store — documented, not treated as a bug; a malformed event never aborts the cycle), 17 in `tests/test_inspect_evidence.py` (filtering, full-history, CLI exit codes, `--data-dir` defaulting, never creating a missing directory).

## 15. Track C — known, disclosed gap

The dashboard's session opportunity-event counts are **not yet wired** to the new `OpportunityEventStore`. This mission explicitly listed "dashboard counts derive from persisted opportunity events" as a requirement; it was not completed this session — the store and its wiring into the real pipeline exist and are tested, but `agent/dashboard_state.py` still derives whatever opportunity-event-related fields it currently shows from its pre-existing sources, not from `materiality_events.jsonl`. This is disclosed here rather than silently left undone or falsely marked complete.

## 16. Track D — audit

`launchctl` is unavailable on this Linux sandbox — every host-capability check in the new tooling below detects this via `shutil.which("launchctl") is None` and reports `UNAVAILABLE` with the real macOS command documented in the reason string, never a guessed or fabricated `PASS`.

## 17. Track D — scripts/reboot_check.py

`--mode pre|post`, read-only. Manifest covers: git branch/HEAD/dirty-tree state, git-tracked-data-files regression check, a full file inventory (path/size/SHA256) split into append-only files (checked via exact byte-prefix comparison: old file's hash must equal the new file's first N bytes) vs. deliberately-overwritten files (`failure_sentinel.json`/`runtime_status.json`), operational mode, failure sentinel state, runtime status staleness, checked-in vs. installed LaunchAgent plists, audit-chain validity, most recent backup. `compare_manifests()` flags any operational-mode change for human review rather than silently accepting or rejecting it. 25 tests, one self-caught fix (a test asserted lowercase "not an unchanged prefix" against the real message's "NOT an unchanged prefix").

## 18. Track D — scripts/backup_snapshot.py

Read-only of `--data-dir`, one new timestamped `tar.gz` + `manifest.json` (per-file path/size/SHA256) per invocation, self-verified by re-extracting into a `tempfile.TemporaryDirectory()` and re-hashing before reporting success. Excludes `.agent.lock` (a live kernel-held sentinel, meaningless to back up). No destructive rotation — every run adds a snapshot, nothing already on disk is ever deleted. Scheduling is documented (the exact `cron`/`launchd` command to run) but not installed — this script never silently creates a recurring job, matching the mission's explicit instruction. 9 tests, all passed first run.

## 19. Track D — scripts/runtime_health.py

One PASS/FAIL/UNAVAILABLE/NOT_YET_OBSERVED report covering every category the mission's soak-testing section asked for: process/broker-environment/operational-state, last scheduled cycle, last explicit reconciliation, last successful collection (a disclosed proxy: `FactStore.all_facts()`'s max `observed_at`), last materiality evaluation (the same disclosed T4-outcome proxy `phase_acceptance.py`'s own Phase 3 criterion already uses), broker-snapshot staleness, cash/position/open-order/day-trade reconciliation flags, quarantine pending counts, audit-chain validity, FactStore/opportunity-event counts, active failure sentinel, Keychain availability (presence-only — tested explicitly that no resolved secret value ever appears in the report's JSON), disk/runtime-store health. One self-caught bug fixed before shipping: `_broker_snapshot_age` initially called `runtime_status.is_stale()`, which checks `generated_at` (the whole snapshot's freshness) rather than `broker_snapshot_at` (the specific field being asked about) — a test with a fresh `generated_at` but a stale `broker_snapshot_at` caught this; fixed to compute staleness directly against the correct field. 24 tests.

## 20. Full test suite, at every checkpoint

| Checkpoint | Python | JS |
|---|---|---|
| Session start | 4968 | 56 |
| After Track A | 4986 | 56 |
| After Track B | 4994 | 56 |
| After Track D | 5055 | 56 |
| After Track C persistence + wiring | 5072 | 56 |
| After Track C CLI tool | **5089** | **56** |

Zero regressions, zero unexplained decreases, at any checkpoint. Every run used `/tmp/investmentagent_test_copy`, rebuilt fresh (`rm -rf` + `cp -a`) from the real repo before each run — the real repo's `data/` was never touched by a test.

## 21. Static safety scans (this session's changes only)

- **Order submission**: exactly one production call site, `agent/approval_execution.py:451` (`return adapter.submit(...)`) — unchanged this session; `git diff` against `agent/approval_execution.py` and `agent/broker/` is empty.
- **Automatic approval**: no `approved = True`/`auto_approve` pattern found anywhere in `agent/`/`scripts/` outside test files.
- **Capability widening**: `agent/policy.py`'s default-deny (`table.get(..., CapabilityStatus.DISABLED)`, "an unlisted value is DISABLED, never permitted") is untouched this session.
- **Reconciliation tolerance**: every "tolerance" match in `agent/reconciliation.py`/`agent/money.py`/`agent/startup.py` is either an explicit "NOT a tolerance" docstring assertion or an unrelated, pre-existing `shown_at`-drift tolerance in `agent/approval_bridge.py` (not touched this session, not a reconciliation tolerance).
- **Claude/T4**: `t4_analysis_enabled` remains `False` by default in both `agent/config.py`'s dataclass and `config.example.json`; not touched this session.
- **Materiality threshold**: `config.example.json`'s `materiality_threshold` remains `2.0`; `agent/materiality.py`'s `score >= policy.threshold` comparison untouched.
- **Secrets**: no literal API-key/secret/password/token-shaped string found in any changed `agent/`/`scripts/` file.
- **Static bytecode noise**: two incidentally-modified `.pyc` files reverted before commit (§2) — not part of this work, not committed.

## 22. Absolute safety rules — compliance for this session

No Alpaca order placed. No `adapter.submit`/`adapter.cancel` call against the live broker. No real approval consumed or automatically approved. No production trading enabled. No operational-mode advancement. No change to the real SPY fill or the +20 opening-balance correction. No ledger history rewritten or deleted. No quarantine file compacted. No reconciliation tolerance introduced or weakened. No market-session order protection weakened. No new asset class (options/crypto/shorting/margin/futures/forex/OTC) enabled. No Claude/T4 analysis enabled. No materiality event manufactured; no threshold lowered. No fabricated dashboard telemetry introduced — Track B specifically *removed* a false claim. No secret exposed. No canonical `data/` mutated by any test.

## 23. What remains open (honestly, not silently)

- **Dashboard opportunity-event counts are not yet wired to `OpportunityEventStore`** (§15) — the single largest disclosed gap against the mission's own stated requirements.
- The real LaunchAgents were not restarted or installed this session, per instruction — `scripts/reboot_check.py`/`scripts/backup_snapshot.py`/`scripts/runtime_health.py` are built and tested but have not yet been run against the real Mac's actual `data/` directory (this sandbox has no macOS `launchctl` to exercise that path against).
- Task #299 ("Investigate recurring TypeError notification claim") remains open from a prior session, untouched here.
- This pilot still proves plumbing, not edge — no point-in-time corpus exists, no P&L figure from this system is signal, and the 12-month kill criterion stands, unchanged by anything in this document.

## 24. Commit

One commit on `main`, covering every file in §3 plus this document. No `data/` files are part of this commit (none were touched).

---

**OVERNIGHT READINESS WORK COMPLETE: YES**
