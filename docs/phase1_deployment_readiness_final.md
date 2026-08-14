# Phase 1 Deployment Readiness — Final Report

Branch: `phase1-integration`. Prepared per the 9-unit review instruction (writer-lock closure, Keychain follow-up, ModeStore resilience, dashboard truth audit round 2, phase-acceptance review, cash-repair re-validation, static safety audit, full validation, deployment plan). Nothing in this document was deployed, merged, restarted, or applied. All absolute rules (no `main` edits, no `data/` mutation, no repair `--apply`, no LaunchAgent actions, no broker writes, no capability changes, no secret exposure) were honored throughout — including two occasions where accidental writes to canonical `data/` were caught and reverted before this document was written (§16).

## 1. Starting state

- Branch point / original `main`: `652055a803789e6763946cad59997d2b10aa57b1`.
- Starting `HEAD` for this session's work: `8713061` (already ahead of the branch point via two prior integration commits, `9fd1c24`/`e303103`, and the writer-lock work already in flight).
- `git branch --show-current` confirmed `phase1-integration` before any edit — the "if not phase1-integration, STOP" condition was never triggered.

## 2. Ending state

- Final `HEAD`: **`ceedc3e4c818e39967e9d19ff5f9be154b8aed9d`**.
- `git status --short` (excluding stale tracked `__pycache__/*.pyc`, a pre-existing repo-hygiene issue unrelated to this work — see §32) is **clean**.

## 3. Commits created this session

| Commit | Subject |
|---|---|
| `a972125` | Unit 1 (round 2): close writer-lock gap for the settled-cash repair script |
| `84c921d` | Unit 2: Keychain caching follow-up — measured evidence at 10 requests, TTL expiry, process-reconstruction granularity |
| `00d9724` | Unit 3: ModeStore tolerates a crash-truncated final row on load |
| `ceedc3e` | Unit 4: dashboard second truth-audit pass — 3 fabricated/missing-telemetry fixes |

(`8713061`, `9fd1c24`, `e303103` predate this session's turn and were already on the branch.)

## 4. Exact files changed

- `scripts/repair_settled_cash_baseline.py` — `--apply` now acquires the canonical process lock before writing.
- `tests/test_repair_settled_cash_baseline.py` — new, 4 tests.
- `tests/test_run_dashboard.py` — 3 new Keychain-cache-count tests; 1 existing test updated for ModeStore's new tolerant-load semantics, 1 new test added alongside it.
- `agent/mode_store.py` — `_load()` tolerates a crash-truncated final row (mirrors `FactStore._load`).
- `tests/test_mode_store.py` — 10 new adversarial tests.
- `dashboard/static/agent_command_center.html` — fixed a hardcoded "Day-trade limit" gate value; added an `operational_state` badge.
- `dashboard/static/approval_card.html` — replaced two fabricated static values with an honest "NOT SHOWN HERE".
- `tests/test_command_center_operational_state_bind.js` — new, 10 tests.
- `tests/test_approval_card_truth_audit.js` — new, 4 tests.
- `docs/phase1_deployment_readiness_final.md` — this file (new).

## 5. Writer-entry-point matrix (Unit 1, carried forward + one correction)

The matrix built in the prior turn (`8713061`) covers every scheduled-loop / one-shot-CLI / dashboard-route writer and is unchanged in substance. **One correction from this session's Unit 5 work**: `agent.diagnostics.diagnose_account` — and every caller of it (`scripts/phase_acceptance.py`, `scripts/diagnose_runtime.py`, and `scripts/run_dashboard.py`'s own `_build_broker_state`, which runs on every `GET /api/state` poll) — was classified "read-only, no lock needed" based on `diagnose_account`'s own body (genuinely read-only) and its docstring's claim. **That classification is now known to be wrong** in one specific way: constructing `CashEventQuarantineStore`/`ExecutionQuarantineStore` — which `diagnose_account` does on every call — has a hidden write side effect (§16). These entry points do mutate `data/cash_quarantine.jsonl`/`data/quarantine.jsonl`, unprotected by the process lock, on every single invocation. This is now the single most important open item in this report — see §16 and §31.

## 6. Writer paths previously covered / now covered

Previously covered (prior turn): scheduled loop, `--submit-approved`, `--admit-execution`/`--reject-execution`, `--admit-cash-event`/`--reject-cash-event`, `--advance-mode-to`, dashboard `POST /api/approval/*`, dashboard `PATCH /api/config`.

Newly covered this session: `repair_settled_cash_baseline.py --apply` (previously the one remaining unlocked genuine writer; now locked, tested, committed).

## 7. Remaining unlocked writer

`scripts/diagnose_broker_state.py` — disclosed, not fixed, in the prior turn (throwaway debug script, no tests, a narrow fresh-ledger seeding path). Still true today; not touched this session.

**Newly disclosed, not fixed**: the quarantine-store self-duplication writes described in §5/§16 are not behind the lock. Fixing this properly means first fixing the underlying `_load_into()` defect (so these stores stop writing on every construction), not just wrapping the existing writes in a lock — locking would only serialize the corruption, not stop it.

## 8. Lock acquisition semantics

Unchanged from the prior turn: `agent.process_lock.acquire_process_lock(data_dir)`, `fcntl.flock(LOCK_EX | LOCK_NB)` on a file inside the canonicalized (`Path.resolve()`) data directory, non-blocking, fails closed (`ProcessLockError`) on contention, released automatically on normal exit, exception, or process death (flock is kernel-held, not a userspace/PID-file convention). This session added one more caller (`repair_settled_cash_baseline.py --apply`) using the exact same primitive; no changes to the primitive itself.

## 9. Crash/SIGKILL test evidence

Carried forward from the prior turn (`tests/test_process_lock.py`'s real-subprocess SIGKILL test, unchanged this session). This session's new lock caller is covered by `tests/test_repair_settled_cash_baseline.py::test_apply_refuses_and_writes_nothing_while_the_scheduled_loop_holds_the_lock`, which proves contention against a real `acquire_process_lock` held by a simulated competing holder, not a mock.

## 10. Keychain lookup counts, before/after

Before (established prior turn): 4 uncached `resolve()` calls per steady-state `/api/state` poll × a 5s poll interval = a real prompt storm over a running dashboard session.

After, measured this session (`tests/test_run_dashboard.py`):
- First request after process start: 1 real resolve per distinct `secret_ref` (cold cache).
- 10 successive steady-state requests: **exactly 1** additional real resolve total (all 10 served from cache).
- A request issued after simulated time crosses the TTL: **more than 0** additional real resolves are forced (proven, not exactly-specified — see §11), demonstrating the cache does expire and is never permanently stale.
- A fresh process (new `CachingSecretsProvider` instance) never inherits a prior process's cached value — proven directly.

## 11. Final Keychain-caching architecture

`CachingSecretsProvider` (already integrated prior turn, reviewed not modified this session): TTL-bounded (default 300s) wrapper around any `SecretsProvider`, keyed by `secret_ref`, `now_fn=time.monotonic` by default (test-injectable). Resolved once per process per TTL window, kept in process memory, reused for ordinary reads, never persisted to disk, never logs the value. Missing-secret failures are **not** cached (a `SecretNotFoundError` is never memoized as a permanent failure — confirmed by source read, not assumed). No change made this session; the review closes the loop with measured evidence instead of source-reading confidence alone.

One measurement was intentionally left as `> 0` rather than an exact count (`test_a_refresh_past_ttl_expiry_re_resolves_all_four_then_caches_again`): the exact number of real re-resolves triggered by a single post-TTL-expiry refresh depends on internal call ordering inside `_build_broker_state`/`select_broker_adapter` that this review did not fully reverse-engineer. The test proves the safety-relevant property (expiry forces at least one real re-resolve, never zero) without overspecifying an implementation detail.

## 12. Expected long-running dashboard Keychain behavior

With the cache in place: at most 4 real Keychain prompts per TTL window (300s default) regardless of poll frequency, not 4 per poll. Over a multi-hour dashboard session this is the difference between roughly 2,880 prompts (4 × 720 five-second polls/hour × hours) and roughly 4 × (hours × 12) — a ~300x reduction, matching the original "prompt storm" finding's own math.

## 13. ModeStore corruption findings, changes, tests

Finding: `ModeStore._load()` had no exception handling — a single crash-truncated final row (a `SIGKILL` between `open(..., "a")` and the completed `write()`/`flush()`/`fsync()` sequence) made the entire `ModeStore()` construction raise, not just lose the one interrupted row. Every real caller already converted that raise into a safe outcome (scheduled loop refuses to start; dashboard/diagnostics degrade to `UNAVAILABLE`), so this was never a path to a fabricated permissive mode — but it was needlessly total.

Change: mirrors `agent.store.FactStore._load`'s already-reviewed pattern exactly. Last line fails to parse, every prior line parses cleanly → tolerated, discarded, recorded on `truncated_tail_on_load`, logged as a warning, load continues; `current()` reports the last **provably durable** row. Any other line fails to parse → still raises (no fsync-ordering argument excuses corruption in the middle of the file). Empty file → unchanged fresh-install baseline.

Required safety property confirmed unchanged: unknown/corrupt mode state can never enable trading. Recovering to "the last state we can prove" is the fail-safe behavior, not a weakening of it, and is not forced to `PAUSED` if the last good row was something else.

10 new tests (`tests/test_mode_store.py`): crash-truncated final row (+ its warning log), malformed middle row (still raises), empty file, missing file, missing-key final row, corrupted-timestamp final row (tolerated) vs. middle row (raises), duplicate consecutive transitions, and confirmation `ModeStore` itself does not validate mode *values* (that's `agent.mode`'s job, downstream, confirmed still true).

One existing dashboard test (`test_run_dashboard.py`) had to be updated: a file with exactly one malformed line is indistinguishable from a crash mid-write of the store's first-ever row, so it's now correctly tolerated rather than raising — the dashboard now reports `operational_state="DISABLED"` (a real, pre-existing, safe value — confirmed via a dedicated subagent trace that this is purely a diagnostic-display change with zero effect on the actual trading gate, which reads `ModeStore` and `agent.mode.assert_legal_startup` directly, never through this dashboard function) instead of the old `(None, None)`. A second test was added alongside it proving genuinely unrecoverable (mid-file) corruption still degrades to `(None, None)`.

## 14. Dashboard truth audit, complete findings (second pass)

An independent second pass (beyond the original truth audit) found three remaining issues, all now fixed:

1. **`agent_command_center.html`, risk-gates panel**: "Day-trade limit" was a hardcoded literal `"0 OF 3"`, unconditionally — live backend or not — never reading `A.reconciliation.day_trade_count`, unlike the correct "Day trades used" row a few lines away in the same file. An operator would see a permanently green "0 of 3" regardless of the real count, including at/near the PDT limit. Fixed to read the real value through `fig()`, exactly like the sibling row.
2. **`operational_state`/`operational_state_paused_from` were never consumed by either dashboard HTML file.** `broker_environment`/`mode` ("PAPER") answers "which broker account"; `operational_state` answers "is trading currently allowed" — independent facts. A PAPER account could be PAUSED and the header said only "PAPER" with zero indication (a gap `docs/unit_e_dashboard_paper_vs_paused.md` had explicitly disclosed as unfixed). Fixed: the header now shows a second badge next to the PAPER pill rendering the real `operational_state` (DISABLED/RESEARCH/PAPER/PRODUCTION_ACTIVE/PAUSED/UNAVAILABLE), each a distinct color, with a tooltip naming what a PAUSED state was paused from.
3. **`approval_card.html` header strip**: "RECONCILED 4 / 4 · 19:58 UTC" and "SPEND MTD $3.42 / $20 · stop $30" were permanently static mockup content — this card's own template has no live data source at all (`A`/`fetch`/`this.state.api` are all absent), and `approval_card_bind.js` never references either figure. Every operator who opened this page saw a specific reconciled count, a specific timestamp, and a specific dollar figure (the same "$3.42" already investigated once before as fabricated, elsewhere), permanently, in a healthy color. Fixed: both spans now read "NOT SHOWN HERE".

No E-classification (misleading/fabricated) items remain in either file after this pass, to the best of this review's ability to check (see §33 for what would still need checking to be fully certain).

One mechanical note: the first attempt at fixing findings 1–2 corrupted `agent_command_center.html` — naive `JSON.stringify()` does not reproduce the original bundler's escaping of `/` as `/` specifically inside `</` sequences (a guard against a literal `</script>` inside the embedded JSON prematurely closing the outer `<script>` tag). This was caught before commit (the file failed to re-decode) and reverted via `git checkout --`; canonical `data/` was never touched by this. The corrected approach re-escapes `</script` after `JSON.stringify()` and round-trips the result back through `JSON.parse` to prove it decodes identically before writing.

## 15. PAPER-vs-PAUSED visibility, confirmed

Confirmed fixed in `agent_command_center.html` (§14, finding 2) — proven both by the new badge's presence and header source-order (`tests/test_command_center_operational_state_bind.js`) and by the color/label matrix across all five real `operational_state` values. `approval_card.html`'s own static "PAPER" text is unchanged and still not distinguishable from `operational_state` — this page has no data path for the field at all (its `applyQueue` contract carries `pending`/`deferred`/`cash` only), and adding one is a larger change than this truth-audit unit's scope. Disclosed, not fixed.

## 16. CRITICAL, newly discovered: quarantine-store self-duplication on every load

**This is the most important finding in this entire review and was not one of the original 9 units — it surfaced as a side effect of Unit 5.**

`CashEventQuarantineStore._load_into()` and `ExecutionQuarantineStore._load_into()` (`agent/cash_event_quarantine.py`, `agent/execution_quarantine.py`) replay every on-disk row **through their own public write methods** (`quarantine()`/`admit()`/`reject()`) rather than populating in-memory state directly. Those methods are idempotent *within a single already-populated instance*, but during replay the in-memory dict starts **empty** and is built up incrementally as replay proceeds — so the first time each row's key is encountered during a fresh load, the idempotency check (`existing is not None`) sees `None` and the write method appends a **duplicate** copy of that row to disk. Reproduced directly, deterministically, in isolation (not against real data): a store with 2 rows on disk grows to 4 after one fresh reconstruction, 6 after a second, 8... — linear growth per reload, not exponential, because within a single reload pass, later duplicate lines for an already-seen key correctly no-op (the dict is populated by then).

**This is not hypothetical or session-induced.** The real, currently-committed `data/cash_quarantine.jsonl` has 786 lines for exactly 2 distinct logical events (one duplicated 394 times, the other 392 times); `data/quarantine.jsonl` has 830 lines for exactly 2 distinct events (duplicated 422 and 408 times). This predates this session — it is the state already on `HEAD` before any commit made in this turn.

**Root cause of why the existing test suite never caught it**: `tests/test_cash_event_quarantine.py::test_quarantine_and_resolution_both_survive_a_reload` (and the equivalent execution-quarantine test) only asserts on the **in-memory, deduplicated** view after a reload (`status()`, `.load()` counts) — never on the raw file's line count. Duplicate rows for the same key collapse to the same dict entry, so the functional-correctness assertions still pass even as the file silently grows underneath them.

**Blast radius, confirmed by source trace**:
- `scripts/run_dashboard.py`'s `_build_broker_state` constructs a fresh `ExecutionQuarantineStore` on **every single `GET /api/state` poll** (every 5 seconds, by explicit design — "Constructed fresh here... an operator's `--admit-execution`/`--reject-execution` must be reflected on this script's very next read"). This means a long-running dashboard process continuously grows `data/quarantine.jsonl` for as long as it runs, with no lock protecting the write.
- `scripts/phase_acceptance.py` and `scripts/diagnose_runtime.py` both call `agent.diagnostics.diagnose_account`, which constructs both stores on every invocation — confirmed by directly running `phase_acceptance.py` against real `data/` during this review (§17) and observing the exact duplication live.
- `LedgerStore._load_into` does **not** have this defect — it replays fills/cash-adjustments through the in-memory `Ledger` domain object's own methods (`ledger.record_fill`, etc.), not through `LedgerStore`'s own disk-appending methods, so no disk write occurs during replay. Real fills, cash adjustments, and opening balances are not at risk from this specific defect. `ModeStore._load`, `FactStore._load`, and `AuditLog._load` are pure read-into-list loaders with no write-capable replay path at all — also not at risk.
- Functional correctness is not affected **today** (every duplicate is byte-identical content for an already-decided event; `status()`/`pending()`/reconciliation reads are unaffected because they read the deduplicated in-memory dict). The actual harm is: (a) unbounded growth of a supposedly append-only evidentiary record with no true new information, undermining its value as an audit trail; (b) uncoordinated, unlocked disk writes from "read" paths that were explicitly relied upon elsewhere in this review (§5) as safe to leave outside the process lock; (c) if this pattern were ever combined with a row whose replay-time validation could fail differently on a second pass (not currently the case, but not provably impossible either), a reload could behave inconsistently with the original write.

**Not fixed in this session.** Fixing it correctly requires either (a) adding a `_loading` guard so `quarantine()`/`admit()`/`reject()` populate in-memory state without appending during replay, or (b) replaying into a separate accumulator and only assigning to `self._quarantined`/`self._resolutions` after the full pass — either way, a real change to two safety-critical append-only stores' core persistence logic, needing its own dedicated tests (including a new file-line-count assertion the existing reload tests are missing) and a decision about whether/how to remediate the already-bloated real files (an append-only-record question squarely inside the absolute rules — "no rewrite, no delete of an existing append-only record" — that is Ray's call, not mine). This is flagged as the top blocker in §31.

Both accidental duplications this defect caused during this review (once from a full-suite pytest run whose exact trigger was never fully isolated before the isolated-copy mitigation was adopted, once directly from running `phase_acceptance.py`) were caught and reverted via `git checkout --` before being committed; `data/` is confirmed clean as of this report (§18).

## 17. `phase_acceptance.py`, read-only verification

Confirmed by source read: reuses `agent.diagnostics.diagnose_account` (not a second, competing reconciliation-reading path); never imports `agent.pipeline`/`agent.approval*`/`agent.pipeline_stage`; never constructs a `Gatekeeper`; the one optional `AlpacaPaperAdapter` it can build has no `capability_policy`/`staging_key` attached, so `.submit()`/`.cancel()` would raise before any network call even if something upstream tried. **Caveat, discovered this session**: "read-only" is true for `phase_acceptance.py`'s own code and for `diagnose_account`'s own body, but **not** end-to-end, because of §16 — running it against a non-empty `data/cash_quarantine.jsonl`/`data/quarantine.jsonl` does write. This was directly observed (and reverted) during this review.

## 18. Current Phase 1 acceptance result (real data, no credentials supplied)

Run without `--key-id`/`--secret-ref` (the "PAPER-safe" mode the script's own docstring describes; no Alpaca credentials were available to this review):

```
alpaca_credentials_present: UNAVAILABLE — no --key-id/--secret-ref given
reconciles_settled_cash: UNAVAILABLE — cannot compare: broker account snapshot not available
reconciles_positions: UNAVAILABLE — cannot compare: broker positions not available
reconciles_open_orders: UNAVAILABLE — cannot compare: broker open orders not available
reconciles_day_trade_count: UNAVAILABLE — cannot compare: broker account snapshot not available
fact_store_has_recorded_at_least_one_fact: NOT YET OBSERVED — facts.jsonl does not exist yet
materiality_screen_has_produced_at_least_one_event: NOT YET OBSERVED — opportunity_events.jsonl does not exist yet
```

Exit code 0 (no `FAIL` — only `UNAVAILABLE`/`NOT YET OBSERVED`, which the harness correctly does not treat as failures of the system, only as "not yet demonstrated"). **No criterion was weakened to produce this result.** A real credentialed run would very likely still show `reconciles_settled_cash` as `FAIL` given the settled-cash mismatch already established (§19) — this review could not verify that directly without credentials.

## 19. Historical cash repair, dry-run output (real ledger, not applied)

Run against the real `data/ledger.jsonl`. **Note: the real ledger's current shape differs from what earlier context described as "expected"** — there is currently no fill row in the real ledger at all (0 local fills, 1 cash adjustment), not the 1-fill shape described in earlier session context. Reporting what is actually there now, not the earlier description:

```
opening_settled_cash (raw, as seeded) = 480
local fills recorded                  = 0
local cash_adjustments recorded       = 1  (CAT fee, -0.01)
existing opening_balance_corrections  = 0
Ledger.settled_cash(now) (BEFORE)     = 479.99
proposed correction_amount            = 0.01
Ledger.settled_cash (AFTER, in-memory only) = 480.00
```

Positions before/after: identical (`{}`/`{}`) — `OpeningBalanceCorrection` never touches `self._fills`, confirmed both by the tool's own in-memory computation and by source read of `record_opening_balance_correction`. Fills before/after: identical (0/0). **Nothing was written; `--apply` was never passed.**

## 20. Ledger SHA256, before and after

`bf8b48b617eccf2ceb8f1aee9f8bd2d6d137d80c2fd33ede809374908c960c50` — identical before and after the dry-run, and identical across every check performed throughout this entire session (multiple independent verifications).

## 21. Repair idempotency evidence

Carried forward from the prior turn's own dedicated test file (`tests/test_repair_settled_cash_baseline.py`, this session): a second `--apply` after a successful first refuses cleanly as a duplicate (exit 0, no second row written) — proven against a real lock and a real `LedgerStore`, not mocked. Not re-run against real data this session (would require `--apply`, prohibited).

## 22. `adapter.submit` call-site count

Exactly **one** production call site: `agent/approval_execution.py:451`. Every other reference found in a repo-wide grep is a docstring/comment discussing that same call site (`agent/pipeline.py:109`, `agent/approval_execution.py:131/158/188/212`) — confirmed by direct inspection, not just a match count.

## 23. Order-path safety chain

Traced statically (not re-derived from scratch — this matches the entry-point-matrix work already committed in prior turns, re-verified this session): the single `adapter.submit` call in `approval_execution.py` is reached only after approval-request validation, deterministic risk gates, the session gate, account binding, signature/bound-token verification, and price/drift checks, and (as of this session) sits inside the process-lock-protected `--submit-approved` path. No new direct submit path was found anywhere in `agent/`/`scripts/`.

## 24. Static secret audit

No hardcoded credentials found (`grep`-based scan for `api_key`/`secret`/`password`/`token = "..."` literal-assignment patterns across `agent/`/`scripts/`, excluding tests: zero matches). No plaintext secret *values* logged anywhere — the only matches for secret-adjacent logging are `secret_ref` (the Keychain **reference name**, never the value) and unrelated uses of Python's `secrets` stdlib module for signing-key generation examples in help text. `tests/test_install_launchagents.py` has a dedicated test (`test_no_raw_secret_value_appears_only_the_opaque_ref_strings_do`) proving no raw secret value can leak through the LaunchAgent installer, by construction.

## 25. Restricted-capability state

`config.example.json`'s `trade_capabilities`: `OPTIONS`/`CRYPTO`/`SHORT_SELLING`/`MARGIN`/`FUTURES`/`FOREX`/`OTC` all `DISABLED`; only `US_EQUITY`/`ETF` `PRODUCTION_ALLOWED`. `sides`: only `BUY`/`SELL` allowed, `SELL_SHORT`/`BUY_TO_COVER` disabled. `funding`: only `SETTLED_CASH` allowed, `MARGIN`/`UNSETTLED_CASH` disabled. `agent/policy.py`'s `CapabilityPolicy` confirms default-deny by source: "Default deny. An unlisted value is DISABLED, never permitted." `t4_analysis_enabled: false` (Claude/T4 analysis disabled by default, confirmed in both `config.example.json` and `agent/config.py`'s dataclass default). Nothing in this session enabled any of the above.

## 26. Python/JS final test counts

- Python: **4928 passed**, 0 failed (up from the 4894 stated as baseline context; the delta is entirely additive new tests from this session's Units 1–4 plus prior-turn work already on the branch — nothing was removed or renamed).
- JS: **56 passed**, 0 failed (up from 42 — 10 new in `test_command_center_operational_state_bind.js`, 4 new in `test_approval_card_truth_audit.js`).
- Both counts were reproduced multiple times across this session, always against a disposable copy of the repo (never the canonical `data/` directory — see §16/§31 for why that discipline mattered).

## 27. Focused test results

All new/modified test files pass in isolation as well as inside the full suite: `test_repair_settled_cash_baseline.py` (4/4), `test_mode_store.py` (28/28, 10 new), `test_run_dashboard.py` Keychain-count tests (3 new, all passing) and the updated ModeStore-corruption test, `test_command_center_risk_gates_bind.js`/`test_command_center_materiality_screen_bind.js` (unchanged, still 15/13 passing against the edited file), `test_command_center_operational_state_bind.js` (10/10, new), `test_approval_card_bind.js` (unchanged, 9/9 against the edited file), `test_approval_card_truth_audit.js` (4/4, new).

## 28. Git status

Clean except stale tracked `.pyc` files under `__pycache__/` (a pre-existing repo-hygiene gap — `.gitignore` for `__pycache__` was added in an earlier unit but these specific files were apparently committed before that and never removed from the index; not touched this session, not a data or safety concern, flagged for Ray's awareness only).

## 29. Proof `data/` is untouched

`git status --short data/` returns no output (clean) as of this report. SHA256 of every file in `data/` recorded in §20/§16 and cross-checked at multiple points throughout this session — always identical to the value first recorded, after every revert. Two accidental writes were caught and reverted during this session (§16); neither was ever committed, and both are fully accounted for above rather than swept under the rug.

## 30. Deploy recommendation

# PHASE 1 DEPLOYMENT READY: NO

## 31. Blockers (why NO)

1. **(Newly discovered, this session — highest priority.)** The quarantine-store self-duplication defect (§16). Any LaunchAgent restart of the dashboard, or any operator running `phase_acceptance.py`/`diagnose_runtime.py` for routine health checks — exactly the kind of read-only diagnostic activity Phase 1 depends on — will continue to silently grow `data/cash_quarantine.jsonl`/`data/quarantine.jsonl` without bound, unlocked, for as long as the system runs. This needs its own dedicated fix-and-test unit, plus a decision from Ray about whether/how to address the already-bloated real files, before this system should run unattended for any extended period.
2. **Settled-cash mismatch, still open.** The dry-run in §19 shows the real ledger currently under-counting settled cash by $0.01 relative to what the repair tool computes as correct (a small amount in absolute terms, but the underlying "opening balance seeded before a related activity was reflected" class of bug is exactly what Phase 1's own exit criterion — "positions, settled cash, open orders and day-trade count reconcile" — is designed to catch). The repair is designed, tested, and dry-run-verified, but not applied (correctly, per the absolute rules) — it needs Ray's explicit `--apply --confirmed` decision.
3. **Phase 1's own acceptance harness cannot currently demonstrate a PASS on any broker-dependent criterion** (§18) — every reconciliation criterion reads `UNAVAILABLE` without live credentials, which this review did not have. A credentialed run is needed to know the real current state, and given finding 2 above, `reconciles_settled_cash` would likely `FAIL` that run today.
4. `scripts/diagnose_broker_state.py`'s disclosed-but-unpatched narrow writer-lock gap (§7) — low severity, but still open.
5. `approval_card.html`'s static "PAPER" text still does not distinguish `operational_state` (§15) — lower severity than the command-center fix in §14/§15, but a real, disclosed gap.

None of the above required — or received — a workaround, a widened tolerance, a skipped gate, or a "just this once" exception. Each is reported as found.

## 32. Exact deployment commands (NOT RUN)

All of the following are for **manual execution by Ray**, only after the blockers in §31 are addressed and this review is superseded by a clean one. Real plist names confirmed present at `deploy/com.investmentagent.reconcile-loop.plist` (Label `com.investmentagent.reconcile-loop`) and `deploy/com.investmentagent.dashboard.plist` (Label `com.investmentagent.dashboard`) — verified by reading the files in this checkout, not assumed from memory.

**A. Final git review**
```sh
cd ~/projects/investmentagent
git fetch origin
git log --oneline main..phase1-integration
git diff main..phase1-integration --stat
```

**B. Merge `phase1-integration` into `main`** (only after A is reviewed and blockers addressed)
```sh
git checkout main
git merge --no-ff phase1-integration
```

**C. Push `main`** (only if B succeeded and Ray has decided to push)
```sh
git push origin main
```

**D. Restart the reconcile-loop LaunchAgent**
```sh
launchctl unload ~/Library/LaunchAgents/com.investmentagent.reconcile-loop.plist
launchctl load ~/Library/LaunchAgents/com.investmentagent.reconcile-loop.plist
```

**E. Restart the dashboard LaunchAgent**
```sh
launchctl unload ~/Library/LaunchAgents/com.investmentagent.dashboard.plist
launchctl load ~/Library/LaunchAgents/com.investmentagent.dashboard.plist
```

**F. Verify both processes running**
```sh
launchctl list | grep com.investmentagent
```

**G. Verify the dashboard HTTP endpoint**
```sh
curl -s http://127.0.0.1:8420/api/state | python3 -m json.tool | head -40
```
(confirm the actual configured port from the plist's own `ProgramArguments` / `--port` flag if different from the placeholder above)

**H. Verify the currently deployed commit**
```sh
git -C ~/projects/investmentagent rev-parse HEAD
```

**I. Cash-repair DRY RUN** (safe, read-only, no `--apply`)
```sh
python3 scripts/repair_settled_cash_baseline.py \
  --ledger-path ~/investmentagent/data/ledger.jsonl \
  --account-id <REAL_ACCOUNT_ID> \
  --now "$(date -u +%Y-%m-%dT%H:%M:%S+00:00)"
```

**J. Apply the cash repair — MUTATING, REQUIRES RAY'S EXPLICIT APPROVAL, NOT RUN BY THIS REVIEW**
```sh
python3 scripts/repair_settled_cash_baseline.py \
  --ledger-path ~/investmentagent/data/ledger.jsonl \
  --account-id <REAL_ACCOUNT_ID> \
  --now "$(date -u +%Y-%m-%dT%H:%M:%S+00:00)" \
  --apply --confirmed
```

**K. `diagnose_runtime.py --no-write`**
```sh
python3 scripts/diagnose_runtime.py --no-write --data-dir ~/investmentagent/data --account-id <REAL_ACCOUNT_ID> ...
```
**Caveat added by this review**: per §16/§17, `diagnose_runtime.py` is not actually side-effect-free against a non-empty quarantine store even with `--no-write` (that flag controls `runtime_status.json`, not the quarantine-store construction inside `diagnose_account`). Treat this command as having the same hidden-write risk as `phase_acceptance.py` until §16 is fixed.

**L. `phase_acceptance.py`, read-only**
```sh
python3 scripts/phase_acceptance.py --account-id <REAL_ACCOUNT_ID> --data-dir ~/investmentagent/data
```
**Same caveat as K.**

**M. Tail relevant logs**
```sh
tail -f ~/investmentagent/logs/reconcile-loop.out.log ~/investmentagent/logs/reconcile-loop.err.log
tail -f ~/investmentagent/logs/dashboard.out.log ~/investmentagent/logs/dashboard.err.log
```

**N. Verify failure-sentinel recovery semantics**
```sh
cat ~/investmentagent/data/failure_sentinel.json
```
(confirm `active`/`recovered_at` fields match the currently-observed process health — see `agent/failure_sentinel.py`'s own docstring for the exact contract)

**O. Verify `runtime_status` eventually comes from a real cycle, not only a diagnostic**
```sh
python3 -c "import json; d = json.load(open('$HOME/investmentagent/data/runtime_status.json')); print(d.get('source'), d.get('generated_at'))"
```
(confirm `source` reflects a real scheduled-loop cycle after the LaunchAgent has been running for at least one full `reconciliation_cycle_interval_seconds`, not just the diagnostic script's own last invocation)

## 33. What evidence would constitute Phase 1 acceptance

Per the acceptance harness's own design (§17–18) and Phase 1's exit criterion (§8.1 Day 3 in the architecture docs: "positions, settled cash, open orders and day-trade count reconcile"): a credentialed `phase_acceptance.py` run (or equivalently, `diagnose_runtime.py`) showing `PASS` on `reconciles_settled_cash`, `reconciles_positions`, `reconciles_open_orders`, and `reconciles_day_trade_count` simultaneously, on a real Alpaca paper account, after the settled-cash correction (§19/§31 blocker 2) has been applied and at least one real reconciliation cycle has run since. Phase 2/3 (`fact_store_has_recorded_at_least_one_fact`, `materiality_screen_has_produced_at_least_one_event`) are separate, later milestones — `NOT YET OBSERVED` on those today is expected and not a Phase 1 blocker by itself.

## 34. What should happen next after Phase 1 passes

Per the custom instructions this whole engagement operates under: nothing about a Phase 1 pass demonstrates profitability — there is no point-in-time corpus and no meaningful sample size, and early P&L must never be described as signal. The 12-month kill criterion already on record stands unchanged. The next steps are: (1) fix §16 properly, with its own tests, before trusting any diagnostic tooling's read-only claims again; (2) apply and verify the cash repair; (3) let the scheduled loop run unattended for a real credentialed cycle and confirm Phase 1's four reconciliation criteria all read `PASS`; (4) only then consider Phase 2/3 work (collectors, materiality screening) as anything more than "wired but unobserved."

## 35. Absolute-rules compliance summary

`main` was never checked out or modified. Nothing was merged or pushed. The settled-cash repair was never run with `--apply`. Neither LaunchAgent was unloaded, bootstrapped, or loaded. No broker write, no Alpaca order submission, occurred at any point (every script this review ran either had no adapter attached or was structurally incapable of `.submit()`/`.cancel()` — verified by source, not assumed). Live mode was never advanced. No quarantine item was admitted or rejected. No live approval was altered. Reconciliation was not weakened; no cash tolerance was introduced (confirmed: `agent/reconciliation.py` still does exact-equality comparison, unchanged). No ledger row was manually changed. No append-only record was deleted or rewritten — the two accidental duplicate-writes this review's own investigation triggered (§16) were reverted via `git checkout --` before being committed, restoring the exact prior committed bytes, not edited into a "corrected" state. No secret was exposed, printed, or logged. PAPER was never changed to production. Claude/T4 analysis was not enabled. No new trading capability was enabled.

## 36. Ambiguities documented, not resolved

None arose this session that required stopping a specific change mid-flight — the one candidate (whether to also wire `operational_state` into `approval_card.html`, §15) was resolved by scoping it out explicitly (the page has no data path for the field at all; adding one is new plumbing, not a truth-audit fix) rather than by guessing at an implementation.

## 37. Work not attempted, and why

Fixing §16 (quarantine-store duplication) was deliberately not attempted in this session: it requires changing core persistence logic in two safety-critical append-only stores, needs new tests the existing suite is missing (a real file-line-count assertion after reload), and raises an append-only-record remediation question for the already-bloated real files that is Ray's decision, not this review's. Doing it hastily alongside nine other units risked exactly the kind of under-tested change to money-adjacent code this engagement's own custom instructions warn against ("tests before implementation on anything touching money, time, or ordering — golden cases, not smoke tests").

---

# PHASE 1 DEPLOYMENT READY: NO
