# Quarantine Store Integrity + SPY Fill Forensic Recovery

Focused unit, 2026-08-14. Supersedes/pauses broad Phase 1 work per the
mission brief. Scope: (A) prove and fix the quarantine-store load-side-effect
defect, (B) map every false "read" path, (C) forensically explain the missing
SPY fill (read-only), (D) prove the normal recovery path offline, (E) report
on the real bloated quarantine files (no remediation performed), (F) audit
git-tracked runtime state (no migration performed), (G) validate on a
disposable copy only. No canonical data file was modified. No cash was
repaired. No fill was manually restored. No mode was advanced.

## 1. Starting branch

`phase1-integration`

## 2. Starting HEAD

`d5035deb8b2aa93421c4f99a6819c84c7e6bfa47` -- **note**: the mission brief
stated an expected HEAD of `ceedc3e4c818e39967e9d19ff5f9be154b8aed9d`. The
actual starting HEAD was one commit ahead: `d5035de` ("docs: Phase 1
deployment readiness final report (Units 1-9)"), my own prior turn's
legitimate final-report commit on this same branch, made before this new
mission's instructions arrived. Branch matched exactly (`phase1-integration`).
This is reported per the brief's own "if the branch or HEAD differs, STOP and
report it" instruction; it was judged benign (my own forward progress on the
same branch, not a rogue or unexpected change) and work proceeded.

## 3. Ending HEAD

`167d95070e2c3815cdcaf0ee81b621573238b8fa`

## 4. Commits created

One commit, `167d950`, on `phase1-integration`. Not merged to main. Not
pushed. Message: "quarantine stores: loading existing state must be purely
read-only" (full body documents root cause, fix, and test evidence).

## 5. Exact files changed

- `agent/cash_event_quarantine.py` -- `_load_into` fix (Unit A)
- `agent/execution_quarantine.py` -- `_load_into` fix (Unit A)
- `tests/test_cash_event_quarantine.py` -- Unit A regression tests
- `tests/test_execution_quarantine.py` -- Unit A regression tests
- `tests/test_fill_sync.py` -- Unit D offline recovery proof

No file under `data/` was changed by this commit or by any other action
taken in this unit. `data/quarantine.jsonl` remains dirty in the working
tree relative to `HEAD` (838 lines vs. `HEAD`'s 830) -- this predates this
unit's work (present at pre-flight capture, growing again mid-investigation
from live dashboard polling; see §16 and §31) and was deliberately left
untouched per the absolute rule against `git checkout`/`restore`/`reset` on
`data/`.

## 6. Quarantine loader root cause

`CashEventQuarantineStore._load_into` and `ExecutionQuarantineStore.
_load_into` replayed every persisted row THROUGH the store's own public,
disk-appending write methods (`quarantine()`/`admit()`/`reject()`) rather
than populating in-memory state directly. Those methods decide "already
known, no-op" by checking `self._quarantined`/`self._resolutions` -- both
empty at the top of every `_load_into` call. So the FIRST occurrence of every
row in the file always looked new to that check and was appended to disk
again, every single time the store was constructed. Logical (deduplicated)
state stayed correct throughout, which is exactly why the existing test
suite's own reload test (`test_quarantine_and_resolution_both_survive_a_
reload`, both files) never caught it -- it asserted only on `.status()`/
`.load()` tuple lengths, never on the raw file's physical line count.

Growth is linear per load (2 lines -> 4 -> 6 -> 8...), not exponential:
within a single replay pass, once the first occurrence of a given id
populates the in-memory dict, any later duplicate-content line for that same
id correctly no-ops for the REST of that pass. It is only the FIRST line for
each id, on EVERY pass, that re-appends.

## 7. Reproduction evidence

Golden reproduction protocol, temp files only (`/tmp`, never `data/`):
write a known 2-line sequence (one quarantine + one ADMITTED resolution),
then reload N times, recording byte hash / physical line count / logical
event count after every load.

Pre-fix code (`git show HEAD:agent/execution_quarantine.py`, restored
temporarily into the disposable copy only, never into the real repo):

| load # | lines | sha256 (12) | logical quarantined | logical resolutions |
|---|---|---|---|---|
| initial write | 2 | -- | -- | -- |
| 1 | 4 | d6d2098f6b05 | 1 | 1 |
| 2 | 6 | e8487f6292f6 | 1 | 1 |
| 3 | 8 | a04a14c6773e | 1 | 1 |
| 4 | 10 | 1a29a9fddabb | 1 | 1 |
| 5 | 12 | df84fe9acc7b | 1 | 1 |

Same pattern independently confirmed for `CashEventQuarantineStore`: initial
2 lines -> 4 -> 6 -> 8 -> 10 -> 12 across 5 loads, logical state stable at
1 quarantined / 1 resolution throughout.

Live confirmation on the real account, during this very investigation:
`data/quarantine.jsonl` grew from 830 lines (git `HEAD`) to 838 lines in the
working tree between the start of this investigation and this section being
written -- 4 more quarantine/resolution pairs, all byte-identical repeats of
the existing SPY execution's own rows, consistent with
`scripts/run_dashboard.py`'s documented 5-second poll cadence constructing a
fresh `ExecutionQuarantineStore` on every `GET /api/state` call.

## 8. Fix design

Replay now applies each row DIRECTLY to the in-memory `_quarantined`/
`_resolutions` dicts via two new private methods per store,
`_replay_quarantine`/`_replay_resolution`, which share the exact same
validation the real write methods (`quarantine()`/`admit()`/
`_record_resolution()`) apply -- cross-account check, BUY/SELL field
requirements on admit (execution store only), unknown-kind/unknown-decision
refusal, "never quarantined"/"already resolved with a different decision"
corruption checks, first-occurrence-wins idempotency for both a restated
quarantine and an identical restated resolution -- but NEVER call
`_append_row`. `_load_into` itself now only reads the file and calls these
two replay methods; it no longer calls `quarantine()`/`admit()`/`reject()`
at all.

Explicitly rejected: a process lock (`agent.process_lock`) around the old
replay. That would only serialize the corruption -- one clean duplicate copy
per load instead of a torn/interleaved one under concurrency -- it would not
stop the file from growing on every single load. The defect is architectural
(replay routed through the disk-appending write path at all), not a
concurrency race, so the fix had to be architectural too.

## 9. Raw line-count before/after tests

From the regression suite (`tests/test_execution_quarantine.py::test_a_
hundred_reloads_append_zero_bytes_and_hash_never_changes` and its
`CashEventQuarantineStore` analog): seed a 4-line file (2 quarantine + 2
resolution rows across two ids), then reload 100 times, asserting line count
after EVERY single reload, not just the last. Line count: 4 before, 4 after
each of the 100 reloads, for both store types. Manually run outside pytest
for the report record (temp files only):

- `ExecutionQuarantineStore`: 2 lines seeded -> 2 lines after 5 sequential
  reloads (fixed code); 2 -> 4 -> 6 -> 8 -> 10 -> 12 (pre-fix code, same
  temp file, same 5 reloads).
- `CashEventQuarantineStore`: 2 lines seeded -> 2 lines after 100 sequential
  reloads (fixed code); 2 -> 4 -> 6 -> 8 -> 10 -> 12 (pre-fix code, first 5
  of the same 100 reloads).

## 10. Hash before/after tests

Same runs as §9, SHA-256 of the full file:

- Fixed code, `CashEventQuarantineStore`: `2bf46f3c2cce...` (12-char prefix)
  identical before the first reload and after all 100 reloads.
- Pre-fix code, same store: hash changes on every single reload (`2bf46f3c
  2cce` -> `c4b6de366a7c` -> `663b2cc5c9ab` -> `7ac192958a96` ->
  `5ad72bd0903c` -> `8b324fd6cb78` across 5 reloads).

## 11. CashEventQuarantineStore test evidence

`tests/test_cash_event_quarantine.py` (new section, "Unit A: load is
read-only"): `test_a_single_reload_appends_zero_bytes`,
`test_a_hundred_reloads_append_zero_bytes_and_hash_never_changes`,
`test_reload_does_not_disturb_logical_state_or_pending_counts`,
`test_quarantine_admit_reject_each_individually_survive_many_reloads`,
`test_a_real_bloated_pre_existing_duplicate_file_collapses_cleanly_and_does_
not_grow` (synthetic 100-line file shaped exactly like the real
`data/cash_quarantine.jsonl`, temp-file only), `test_repeated_diagnostic_
style_construction_does_not_mutate_the_file` (20 simulated `diagnose_
account`-style polls). All pass; all existing tests in the file continue to
pass unmodified.

## 12. ExecutionQuarantineStore test evidence

`tests/test_execution_quarantine.py`, identical six-test section, plus the
BUY/SELL field-requirement validation is exercised through `_replay_
resolution` specifically (not just `admit()`) by the bloated-duplicate-file
test, which contains a real ADMITTED resolution row requiring
`holding_policy_version` and no `lot_id`. All pass; all existing tests in the
file continue to pass unmodified.

## 13. False-read caller matrix

| Caller | Why constructed | Expected read/write | Side effect BEFORE fix | Side effect AFTER fix | Writer lock needed after fix? |
|---|---|---|---|---|---|
| `agent/diagnostics.py::diagnose_account` (both stores) | Read pending/status for a diagnostic snapshot | Read-only | Appended a duplicate copy of every row on every call | Pure read | No |
| `scripts/phase_acceptance.py` (via `diagnose_account`) | Read-only acceptance check | Read-only | Same as above (inherited) | Pure read | No |
| `scripts/diagnose_runtime.py` (via `diagnose_account`) | Read-only runtime diagnostic | Read-only | Same as above (inherited) | Pure read | No |
| `scripts/run_dashboard.py::_build_broker_state` (execution store only) | Fresh construction on every `GET /api/state` poll (5s cadence, by explicit design comment) so an operator's `--admit-execution`/`--reject-execution` is visible on the very next poll | Read-only | Appended a duplicate copy of every row on every poll -- confirmed live, the direct cause of `data/quarantine.jsonl` growing from 830 to 838 lines during this investigation | Pure read | No |
| `scripts/diagnose_broker_state.py` (execution store only) | Throwaway diagnostic reproducing `_build_broker_state`'s body against real credentials/data, with the try/except removed, to surface a swallowed exception (per the script's own docstring) | Read-only | Same duplication bug | Pure read | No |
| `agent/run_loop.py::run_cycle` (both stores) | Constructed fresh every scheduled cycle; feeds `sync_fills`/`sync_cash_events`, which DO legitimately call `quarantine()`/`admit()`/`reject()` | Read at construction, write during the cycle via `sync_fills`/`sync_cash_events` | Construction ALSO appended a duplicate of every existing row, on top of any legitimate new write that cycle | Construction is pure read; only the legitimate `sync_fills`/`sync_cash_events` calls append | Yes, at the write call sites (already covered by the prior unit's writer-lock work; see `agent/process_lock.py`) |
| `scripts/run_agent.py` `--admit-execution`/`--reject-execution` and `--admit-cash-event`/`--reject-cash-event` | One-shot operator CLI; the real, intended mutation path | Read at construction, then exactly one intended write | Construction ALSO appended a duplicate of every existing row before the operator's own intended write even happened | Construction is pure read; only the operator's own `admit`/`reject` call appends | Yes (already wired; `scripts/run_agent.py` is in the writer-lock caller set) |

`agent.cash_events.sync_cash_events` and `agent.account_wiring.build_
account_reconciliation` were checked and do NOT construct their own store --
both take an already-constructed store as a parameter, so they are not
additional call sites.

## 14. Confirmation dashboard GET is read-only after fix

`scripts/run_dashboard.py::_build_broker_state` constructs `ExecutionQuarantineStore` fresh on every poll and calls only `pending_count()`/read
accessors on it before handing it to `build_account_reconciliation` (also
read-only on this store). With the fix, construction alone provably appends
zero bytes (§9-§12); no `quarantine()`/`admit()`/`reject()` call exists
anywhere in the dashboard's request path. Confirmed directly by `tests/
test_execution_quarantine.py::test_repeated_diagnostic_style_construction_
does_not_mutate_the_file`, which simulates 20 rapid polls against a shared
file and asserts the hash never moves.

## 15. Confirmation diagnostics are read-only after fix

`agent/diagnostics.py::diagnose_account` constructs both stores and calls
only `status()`/`pending()`/`pending_count()`/`load()` -- no mutation method
anywhere in that function. Same test evidence as §14 applies identically
(the test's own docstring names `diagnose_account` explicitly). `scripts/
phase_acceptance.py` and `scripts/diagnose_runtime.py` inherit this
guarantee, since both call `diagnose_account` and touch neither store
directly.

## 16. Current real ledger shape

`data/ledger.jsonl`, 2 lines, unchanged throughout this entire unit:

```
{"kind": "cash_adjustment", "adjustment_id": "20260728000000000::de3745eb-7d16-4bf3-9514-234693d9f84e", "account_id": "PA3XZX944LRR", "amount": "-0.01", "activity_type": "FEE", "description": "CAT fee for proceed of 1 trades on 2026-07-28 by PA3XZX944LRR", "effective_date": "2026-07-28", "symbol": null}
{"kind": "opening_balance", "amount": "480", "at": "2026-08-12T16:06:48.185029+00:00"}
```

Zero fills. Zero order records.

## 17. Current ledger SHA256

`bf8b48b617eccf2ceb8f1aee9f8bd2d6d137d80c2fd33ede809374908c960c50` -- verified
identical at pre-flight, at multiple points during this investigation, and
in the final validation pass (§35).

## 18. Committed HEAD ledger shape/hash

Byte-identical to §16/§17. `git show HEAD:data/ledger.jsonl | sha256sum` ==
`bf8b48b617eccf2ceb8f1aee9f8bd2d6d137d80c2fd33ede809374908c960c50`. Only one
commit in this file's ENTIRE git history has ever touched it: `e93696b`,
2026-08-12 11:40:49 -0500, message "push" -- and that commit's own content
is byte-identical to the current committed content. This file has never been
git-committed WITH a fill in it, ever.

## 19. Forensic SPY-fill timeline

| When | Event | Source |
|---|---|---|
| 2026-08-12 11:40:49 | Commit `e93696b` ("push") writes `data/ledger.jsonl` in its current 2-row, 0-fill shape. The only commit in this file's history. | `git log --all --follow -- data/ledger.jsonl` |
| 2026-08-12 16:06:48.185029 | The SPY execution (`20260728104251412::37042727-...`, filled 2026-07-28T14:42:51.412408Z) is quarantined -- a BUY with no `holding_policy_version` staged. Opening balance of $480 is also established at this same instant (both derive from the same broker read). | `data/audit.jsonl` seq 3 (`execution_quarantined`); `data/ledger.jsonl`'s own `opening_balance` row |
| 2026-08-12 23:35:29.473928 | Operator admits the execution, supplying `holding_policy_version="config"`. | `data/audit.jsonl` seq 9 (`execution_admitted`, actor "operator") |
| (sometime after 23:35:29, exact instant not captured in any durable audit record -- see gap noted below) | The real Fill is legitimately written to the WORKING-TREE `data/ledger.jsonl` by `sync_fills` (the normal path an ADMITTED resolution takes on its very next poll, per `agent/fill_sync.py`'s own documented behavior -- see §23). This version was never git-committed. | Reconstructed from my own first-hand observation in the immediately preceding session: a `git diff data/ledger.jsonl` showed `index f9e0ad5..f41b7e6`, with the added line being exactly the SPY fill row (`fill_id` matching the execution id, `qty=0.027087234`, `price=737.986`, `lot_id` = the execution id, `holding_policy_version="config"`) |
| (same preceding session, shortly after) | `git checkout -- data/ledger.jsonl` is run (as part of reverting an unrelated, believed-to-be-accidental change under that session's then-current rules, which permitted this and have SINCE been revised specifically because of this incident) | My own recollection; corroborated below |
| 2026-08-14 15:14:44 (this session, before this investigation began) | `data/ledger.jsonl`'s filesystem mtime, per `stat` | `stat data/ledger.jsonl` |
| 2026-08-14 (this unit) | Ledger observed at 0 fills, content byte-identical to git blob `f9e0ad5` (the "before" side of the diff above, and the same blob as the sole historical commit `e93696b`) | This unit's own forensic reads |

## 20. Whether git checkout/restore caused the fill disappearance

**PROVEN.**

## 21. Evidence supporting that conclusion

1. **Blob-hash match.** The working tree's current git blob for `data/
   ledger.jsonl` (`git ls-files -s` -> `f9e0ad54a8cf8430d8f8d441866bde630
   d6f724c`) is identical to the blob committed in `e93696b` (the file's
   only-ever commit), and identical to the "before" side of the diff
   (`index f9e0ad5..f41b7e6`) I personally observed and reverted in the
   immediately preceding session. `f41b7e6` -- the "after" side, containing
   the SPY fill -- was never committed and no longer exists anywhere in
   this repository's git object store.
2. **Single-commit history.** `data/ledger.jsonl` has exactly one commit in
   its entire history, dated 2026-08-12 11:40:49, well BEFORE the
   quarantine (16:06:48), the admission (23:35:29), and any fill-sync write
   that followed. No commit exists that could have introduced, or later
   removed, a fill -- because the file was never re-committed at all after
   that first commit. The fill's disappearance cannot be explained by any
   git history operation OTHER than a working-tree-level checkout/restore
   silently replacing the (uncommitted) fill-containing version with the
   (committed) fill-free version.
3. **Filesystem timestamp is consistent with a checkout, not a fill-sync
   write.** The file's mtime (2026-08-14 15:14:44) is a full two days after
   the last plausible fill-sync write (2026-08-12, shortly after the
   23:35:29 admission) and falls in a window matching this session's own
   remediation activity, not any broker-polling activity.
4. **No reflog entry exists for a ref-changing operation at that time.** A
   bare `git checkout -- <path>` (or `git restore <path>`) does not move
   `HEAD` and therefore creates NO reflog entry -- exactly what `git
   reflog` shows: no entry between 14:08 and 14:37 on 2026-08-14 covering
   a working-tree-only file checkout, only the ref-moving commits either
   side of it. This is consistent with, not contrary to, a bare path-level
   checkout having occurred.
5. **The admission was real, not a test artifact.** `data/audit.jsonl`
   seq 3/9 are genuine, hash-chained audit entries with `actor: "operator"`
   for seq 9 -- a real human decision, not something a test suite could
   have produced (no test in this codebase writes to `data/audit.jsonl`
   directly; all tests use `tmp_path`).
6. **My own repair-script test fixture, checked, could not be the source.**
   `scripts/repair_settled_cash_baseline.py`'s test fixture (`_seed_real_
   incident_shape`) was explicitly modeled on this same real incident's
   shape, but every test in this codebase -- without exception -- uses
   `tmp_path`, never the real `data/` directory, so it cannot have written
   to the real ledger.

**Acknowledged gap, stated honestly**: no durable audit-log entry exists for
the fill-write event itself (`sync_fills` does not emit an audit action on a
successful write -- confirmed by `data/audit.jsonl`'s complete action
distribution, which contains no `fill_recorded`/`fill_synced`-shaped entry
anywhere in its 401 rows). The exact instant of the fill-sync write is
therefore reconstructed from first-hand recollection of the diff I observed
and reverted, not from an independently-timestamped durable record. This is
the one piece of the timeline that rests on my own memory of the prior
session rather than a re-derivable artifact; every other link in the chain
above is independently verifiable from git objects, the audit log, or the
filesystem as they exist right now.

## 22. Current quarantine status of the SPY execution

Still quarantined, and still ADMITTED. `data/quarantine.jsonl` (post Unit A
fix, logically collapsed from its 838 physical duplicate lines): exactly one
quarantine row and one resolution row for `20260728104251412::37042727-
dfba-4cac-a1d7-607636cd4346`, decision `ADMITTED`, `decided_by: "operator"`,
`decided_at: 2026-08-12T23:35:29.473928+00:00`, `holding_policy_version:
"config"`, `lot_id: null`.

## 23. Whether normal fill_sync can recover it

**Yes**, mechanically, with no special-casing anywhere. Traced directly in
`agent/fill_sync.py::sync_fills`: for each broker-reported execution, it
first checks `fill_id in known_ids` (derived from the LEDGER's own persisted
fills, not from the quarantine store) -- since the ledger currently has zero
fills, this check does not block a resync. It then checks `quarantine.
resolution_for(fill_id)`; an `ADMITTED` resolution causes it to build and
write the `Fill` immediately (`store.write_fill(fill)`), using the resolution's
own `holding_policy_version`, subject to every validation `Ledger.record_fill`
already enforces. Nothing in this path is specific to SPY, to this account,
or to this incident.

## 24. Offline recovery test result

`tests/test_fill_sync.py::test_normal_fill_sync_recovers_the_admitted_
execution_with_no_special_case` (Unit D): a disposable `tmp_path` fixture
reproduces the real durable evidence exactly (opening balance $480, the real
CAT fee cash adjustment, the real quarantine+ADMITTED-resolution pair, a
`FakeBroker` reporting the real execution) and calls the real, unmodified
`sync_fills`. Result: **PASS**. Exactly one `Fill` is written, matching the
real `fill_id`/`symbol`/`side`/`qty`/`price`/`holding_policy_version`
exactly; a second `sync_fills` call returns no new fills (idempotent); the
resulting `Ledger.positions()` is exactly `{"SPY": Decimal("0.027087234")}`;
`Ledger.settled_cash()` is exactly `Decimal("460.00")`. No ledger row was
ever touched by hand anywhere in the test.

**Correction (2026-08-14, follow-up review)**: this $460.00 is the LOCAL
ledger's own internally-derived figure from the fixture's own inputs
(opening $480 + the fixture's CAT fee + the fixture's fill cost) -- it is
NOT a confirmed match against the broker's own current settled cash, and
the original version of this report incorrectly treated it as one. The
real broker's most recent authoritative read (this morning, 2026-08-14)
reported settled cash of **$480.00**, not $460.00. A fresh, authoritative
broker read taken AFTER the real SPY fill is recovered is required before
any conclusion about a correction can be drawn -- see §27-§28 (revised)
and §37-§38 (revised) below.

## 25. Expected ledger after recovery

One additional `fill` row: `fill_id` = the real execution id, `symbol=SPY`,
`side=BUY`, `qty=0.027087234`, `price=737.986`, `lot_id` = the execution id
(BUY: lot_id is the fill_id itself), `holding_policy_version=config`. The
existing opening-balance and cash-adjustment rows are untouched (append-only).

## 26. Expected position after recovery

`SPY = 0.027087234` -- exactly the quantity the original, now-discarded
working-tree fill carried, and exactly what §21's forensic reconstruction
independently arrived at from the quarantine record alone.

## 27. Expected settled cash after recovery (REVISED -- see correction below)

**LOCAL ledger figure only, not a broker-confirmed value.** Once the SPY
fill is legitimately recovered, the LOCAL ledger's own `settled_cash()`
computes to exactly `$460.00` (opening $480.00, minus the CAT fee $0.01,
minus the BUY's cost $19.99 -- `0.027087234 * 737.986 = 19.989999470724`,
posted at USD-cent precision via `Ledger._cash_notional`'s banker's
rounding -- `480.00 - 0.01 - 19.99 = 460.00` exactly). This is a fact about
what the LOCAL bookkeeping formula produces from the LOCAL ledger's own
recorded inputs; it is **not** the same thing as confirming the broker's
own account actually agrees with that figure.

**Correction, this follow-up review**: the original version of this report
treated the $460.00 local figure as if it were also the broker's own
expected settled cash, and independently cited an earlier unit's own
"$460.00 traced from real persisted data" finding as convergent
confirmation. That earlier finding was itself a LOCAL/dashboard-derived
figure, not an authoritative broker API read taken after this recovery, so
citing it as confirmation was circular, not independent. The most recent
actual credentialed broker read (this morning, 2026-08-14) reported
**settled cash = $480.00** -- a full $20.00 above the local $460.00 figure.
The local $460.00 computation is not wrong on its own terms (§24's
arithmetic is correct), but it is NOT safe to assume it equals what the
broker will report once the fill is recovered there too. **The broker's own
expected post-recovery figure is unknown until it is actually read from
the broker, fresh, after recovery** -- see the revised procedure in §28.

## 28. What historical cash correction would then be required, if any (REVISED)

**Unknown until measured -- do NOT assume $0.00 or any other figure.** The
correct procedure, none of which has been executed:

1. Recover the SPY fill through the normal, unmodified `sync_fills` path
   against the REAL broker (§23-§24 already prove this mechanism works
   offline; it has not been run against the real account in this unit).
2. Take a FRESH, authoritative broker account read (real credentials, real
   API call) immediately after that recovery -- not a cached or
   morning-stale figure.
3. Compare that fresh broker settled-cash figure against the LOCAL ledger's
   own post-recovery `settled_cash()` (expected to compute to $460.00 per
   §27, from the local ledger's own inputs alone).
4. Only THEN determine whether a correction is needed, and if so, exactly
   how much.

**A named contingency, not a conclusion**: if the broker's own settled cash
remains at $480.00 even after the SPY fill is recovered locally, the gap
would be $480.00 - $460.00 = **$20.00**, not the $0.01 this report
previously (and separately, still correctly-retracted) considered. A
$20.00 gap of this shape -- the broker's own reported cash sitting exactly
above the local, fill-inclusive figure -- is consistent with the
PREVIOUSLY PROVEN baseline-double-count failure mode this same account hit
before (see the earlier "Fix cash-seed ordering" / opening-balance-
double-count units of this engagement): an opening balance seeded from a
broker read that already reflected an activity the local ledger later ALSO
recorded independently. This is named here as the leading candidate
explanation for a persistent $480 broker figure, NOT as a confirmed
diagnosis -- it has not been re-verified against this specific SPY
incident's own timeline, and doing so is explicitly out of scope for this
report (no correction is being computed, applied, or recommended here).

A prior dry-run of `scripts/repair_settled_cash_baseline.py` against the
CURRENT (0-fill) ledger computed a proposed `+$0.01` correction (to move
settled cash from $479.99 to $480.00) -- per this mission's own explicit
instruction, that figure was NEVER applied and must now be discarded: it
was an artifact of the fill being erroneously absent from the LOCAL ledger,
not a real discrepancy against the broker. It does NOT follow that the
correction is therefore $0.00 either -- that would repeat the same error
(assuming an answer instead of measuring it) in the opposite direction. The
only two numbers on the table right now are: the local, fill-recovered
ledger's own internal figure ($460.00, §27), and this morning's real broker
read ($480.00) taken BEFORE recovery. Neither one, by itself, tells us what
the broker will report AFTER recovery, or whether the two will then agree.
No correction is computed, applied, or recommended in this report. See §38
for the exact next steps.

## 29. Real quarantine duplicate statistics

Pure read-only line-by-line inspection (no store construction):

| File | Physical lines | Logical quarantined events | Logical resolutions | All duplicates byte-identical? | Order | Conflicting resolutions? |
|---|---|---|---|---|---|---|
| `data/quarantine.jsonl` | 838 | 1 (the SPY execution) | 1 (ADMITTED) | Yes | Not interleaved -- both ids' rows are internally consistent groups | None |
| `data/cash_quarantine.jsonl` | 786 | 1 (one cash activity) | 1 (ADMITTED) | Yes | Same | None |

Audit interpretation remains fully deterministic: first-occurrence-wins
replay converges to the same final logical state regardless of how many
identical duplicate copies exist -- proven directly by this unit's own
"real bloated duplicate file collapses cleanly" regression tests (§11-§12).
Performance at current size is a non-issue (sub-millisecond parse either
way) and, after the Unit A fix, these files will never grow further from
loading alone.

## 30. Remediation recommendation for bloated files

**Option A: preserve the existing files forever as historical evidence; take
no further action beyond the Unit A code fix.** Justification: every
append-only store in this codebase (`LedgerStore`, `AuditLog`, `ModeStore`,
both quarantine stores) has exactly this posture already -- none of them
have ever had a compaction mechanism, by design, matching the project's
explicit append-only/never-rewrite-history invariant (the same invariant
this very mission's absolute rules enforce by forbidding "compact/rewrite/
dedupe the real quarantine files" in this unit). The Unit A fix already
guarantees these files will never grow again from ordinary loading; their
existing bloat is now permanently inert. A future operator inspecting
`data/quarantine.jsonl` by eye will see ~400 duplicate pairs, which is a
minor readability nuisance, not a correctness or performance risk. If that
readability concern becomes a real operational annoyance, a lightweight
secondary option is a read-only, clearly-labeled ARCHIVE COPY generated
alongside the original (never replacing it) -- but this is optional, not
required, and was not designed further here since it was explicitly out of
scope for this unit ("do not perform the remediation").

## 31. Git-tracked runtime-state findings (STATUS: MITIGATED, follow-up unit)

At the time this section was originally written, `git ls-files data/`
returned all six files then in `data/`: `audit.jsonl`,
`cash_quarantine.jsonl`, `failure_sentinel.json`, `ledger.jsonl`,
`mode_state.jsonl`, `quarantine.jsonl`. **Every single one was live,
continuously-mutating runtime state, not a fixture or seed file.** This was
the exact structural defect class that caused the SPY fill's disappearance
(§19-§21): because these files were git-tracked, ANY `git checkout`/
`restore`/`reset`/branch-switch/merge touching `data/` would silently
overwrite live runtime state with whatever was last committed, discarding
any uncommitted writes since. This risk was not unique to `ledger.jsonl` --
the same operation against `mode_state.jsonl` could have silently reverted
a real mode transition; against `audit.jsonl`, silently truncated the
hash-chained audit trail; against `failure_sentinel.json`, silently erased
active-failure state.

**As of the follow-up unit (Task 1, 2026-08-14): `git ls-files data/` now
returns nothing.** This exposure is closed -- see §32.

## 32. Recommendation for preventing future source-control clobbering (IMPLEMENTED, follow-up unit)

Originally reported as design-only ("do not remove anything from git yet").
**Implemented in a follow-up unit, 2026-08-14 (Task 1)**, once explicitly
authorized:

1. `.gitignore` already contained a blanket `data/` rule (predates this
   whole engagement) -- confirmed still present, no edit needed. This is
   what stops any of these files from being RE-tracked by a future `git add
   .`; it does not, by itself, untrack a file already in the index.
2. `git rm --cached` run on all six previously-tracked files -- index-only;
   the working-tree copy of every file was confirmed byte-identical (SHA256)
   immediately before and immediately after. `git ls-files data/` now
   returns nothing.
3. No seed/fixture file is required for a fresh checkout: every store's own
   `__init__` already tolerates a missing FILE (`if self._path.exists():
   self._load_into()`) and creates it lazily on first write via
   `_append_row`'s `open(path, "a", ...)`. **One real nuance found and
   confirmed empirically** (not present in the original design-only
   version of this section): a truly fresh `git clone` has no `data/`
   DIRECTORY at all (git only materializes directories for tracked
   content), and a bare `Path(path).open("a", ...)` fails with
   `FileNotFoundError` if the parent directory doesn't exist -- confirmed
   by direct reproduction. This is NOT a live gap: both real entry points,
   `scripts/run_agent.py` (line ~1444) and `scripts/run_dashboard.py`
   (line ~434), already call `Path(args.data_dir).mkdir(parents=True,
   exist_ok=True)` at startup, before any store is constructed -- confirmed
   by empirically cloning the repo fresh, running that exact mkdir step,
   then constructing a store and writing to it successfully with no
   pre-existing file or directory. The gap only exists if a store is
   hand-constructed directly against a path whose directory has never been
   created by anything -- not a path any real entry point takes.
4. Backup/durability for this now-untracked runtime state remains a
   filesystem-level concern separate from git (unchanged recommendation,
   not implemented -- e.g. a periodic dated `tar`/`rsync` snapshot, or the
   host's own backup mechanism).
5. `tests/test_repository_hygiene.py` (new) added as a permanent regression
   guard: asserts no file in `RUNTIME_FILES` is ever `git ls-files`-tracked
   again, and that `.gitignore` still contains its blanket `data/` rule.

## 33. Python final test count (updated, follow-up unit)

**4943 passed** (disposable copy only; `/tmp/investmentagent_test_copy`, a
fresh `cp -a` of the real repo taken immediately before this final run).
Baseline going into the original unit was 4928; original unit added +13
(reaching 4941); this follow-up unit added +2 more
(`tests/test_repository_hygiene.py`, Task 1) for a final total of 4943.
Zero removed, zero newly failing, across both units.

## 34. JS final test count

**56 passed**, unchanged from the 56 baseline throughout both units. No JS
files were touched by either unit.

## 35. Canonical data hashes before/after this work (updated, follow-up unit)

| File | Original unit before | Original unit after | Follow-up unit (Task 1) before | Follow-up unit after |
|---|---|---|---|---|
| `data/audit.jsonl` | `8344648609f8e02e764eeb9b9c57e216d1877f47b42158404ce772b8a5234ddb` | identical | identical | identical |
| `data/cash_quarantine.jsonl` | `1239f5deec15801ef5b3dae4923b0a7ae1ba9c8e8c2ee17652c613ed5ad5387f` | identical | identical | identical |
| `data/ledger.jsonl` | `bf8b48b617eccf2ceb8f1aee9f8bd2d6d137d80c2fd33ede809374908c960c50` | identical | identical | identical |
| `data/mode_state.jsonl` | `a5569905490189861eca5a2006ac592f343a25809b7798be6e01ab70852d8655` | identical | identical | identical |
| `data/quarantine.jsonl` | `218966bc3567487fd9c2699875a7dfff023da9c024d4fc9948870990c3b8cf71` (838 lines, dirty vs. HEAD's 830 -- pre-existing at pre-flight) | identical | identical | identical |
| `data/failure_sentinel.json` | `3deb14a058b30a463b30426643863e468e99887dea9f0e06893aabda16fe3444` | identical | identical | identical |
| `data/runtime_status.json` | `5e4f03c1e7244cd1ddd22ffb2b9782d5e68c11c43e88572e7c90ea4fff95f870` | identical | identical | identical |

Every hash has remained byte-identical across BOTH units, including across
the `git rm --cached` operation in Task 1 (an index-only change, verified
by hashing immediately before and immediately after that specific command,
in addition to the before/after-full-suite checks). `git ls-files data/`
now returns nothing (was: all 6 files except `runtime_status.json`, which
was already untracked). No canonical file changed unexpectedly at any
point in either unit; nothing was ever "restored" with git, because nothing
needed to be.

## 36. Confirmation no real data was modified

Confirmed by §35, across both units. `git status --short` on the real repo
shows only source/test/doc files as modified (the original unit's 5 files
plus `docs/quarantine_integrity_and_spy_forensics.md` itself; this
follow-up unit's `tests/test_repository_hygiene.py` plus the 6 staged
deletions from the git index), plus the pre-existing (untouched, unstaged)
dirty `data/quarantine.jsonl`, plus unrelated `__pycache__`/`.DS_Store`
churn. No file's CONTENT under `data/` was ever staged, committed, or
altered by any command run in either unit -- Task 1's `git rm --cached` is
the one operation that touched the git INDEX for these files, and it is by
construction incapable of touching file contents (it is documented,
verified, and enforced by the new hygiene test to never do so again by
accident). Every test run touched only `tmp_path` fixtures or the fully
disposable `/tmp/investmentagent_test_copy`.

## 37. Deployment recommendation

**NO -- not yet.** Unchanged conclusion, now on firmer footing (the cash
conclusion correction in §27-28 removes a premature assumption this
recommendation must not be based on). The quarantine-store fix itself is
tested and ready to ship. The git-untracking migration (§31-32) is now
implemented and verified safe. Neither of those is sufficient on its own:
this unit deliberately did not restart any process, did not run
`sync_fills` against the real broker, and did not repair cash. The next
section names the concrete steps still needed before this can go live --
notably, step 2 is now a HARD PREREQUISITE for any cash decision, not an
optional nice-to-have.

## 38. Exact next safe operational steps (REVISED)

1. Review and merge `167d950`/`c3a3745` (or equivalent commits) to `main`
   when ready -- not done here, per the absolute rules.
2. Restart the dashboard/agent LaunchAgents to pick up the fix -- not done
   here, per the absolute rules ("do not restart LaunchAgents").
3. Run a real `sync_fills` cycle against the live broker (not this unit's
   offline fixture) to let the SPY execution recover through the now-fixed,
   ordinary path -- explicitly NOT done here ("do not manually restore the
   SPY fill" and "do not apply the settled-cash repair").
4. **Immediately after that real recovery, take a FRESH, authoritative
   broker account read** (not the morning's stale $480.00 figure) and
   compare it against the local ledger's own post-recovery
   `settled_cash()` (expected $460.00 per §27, from local inputs alone).
   Do **not** assume they will agree.
5. Only after that comparison exists: decide whether a correction is
   needed, and if so, exactly how much (the named $20.00 contingency in
   §28 is a candidate hypothesis to investigate against the
   previously-proven baseline-double-count failure mode, not a number to
   apply on sight). No correction is computed, applied, or recommended by
   this report.
6. Decide on, and if desired, implement the Unit E archive-copy option
   (§30) -- design only, and likely unnecessary.

## 39. Whether Phase 1 can resume immediately after this fix

Yes, from the quarantine-store-integrity standpoint -- that defect is
root-caused, fixed, and regression-tested, and the git-untracking migration
that prevents a recurrence of the SPY-fill-loss failure mode is now
implemented too. Phase 1 deployment readiness (`docs/
phase1_deployment_readiness_final.md`) should NOT, however, be treated as
unblocked on the cash question -- §37-38's revised procedure (fresh broker
read after recovery, no assumed figure) must complete first if Phase 1's
own readiness assessment depends on a reconciled settled-cash state.

## 40. Remaining blockers

- The live SPY fill has not yet been recovered on the real account (by
  design, per this unit's own read-only-for-cash mandate) -- §38 step 3.
- **The historical cash correction, if any, is UNKNOWN** -- not $0.00, not
  $0.01, not assumed $20.00 either -- pending a fresh authoritative broker
  read taken AFTER real recovery (§27-28, §38 steps 3-5). This is now the
  single largest open question blocking any cash-related sign-off.
- `data/quarantine.jsonl` and `data/cash_quarantine.jsonl` remain bloated
  (§29-§30) -- inert, not blocking, but still present.
- The dashboard/agent processes have not been restarted, so the live
  system is still running the pre-fix code until that happens.
- The git-untracking migration (§31-32) is implemented and verified but has
  not yet been merged to `main`.

QUARANTINE INTEGRITY UNIT COMPLETE: YES
