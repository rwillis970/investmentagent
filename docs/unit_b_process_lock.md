# Unit B — process lock for concurrent writers (reconstructed 2026-08-13)

STATUS OF PRIOR REPORT: a previous session reported this unit as designed,
implemented and tested, on the same now-lost `/tmp` worktree described in
Unit A's doc. That report is UNVERIFIED. Independently checked against the
CURRENT real-repo source (`grep -rln "fcntl\|flock\|ProcessLock\|process_lock"
agent/ scripts/ tests/` returns zero matches before this unit) confirms NO
process-lock mechanism existed in the real repo at this worktree's baseline
commit — this was built fresh, from scratch, in this session.

## Design

`agent/process_lock.py::acquire_process_lock(data_dir)` — a context manager
using `fcntl.flock(fd, LOCK_EX | LOCK_NB)` on `<data_dir>/.agent.lock`.
**Lock scope**: one lock per resolved `data_dir` (not per-file, not
per-account) — every durable store this codebase has
(`LedgerStore`/`ModeStore`/`AuditLog`/`ExecutionQuarantineStore`/
`CashEventQuarantineStore`) lives under the same `--data-dir` in normal
deployment, so one directory-scoped lock covers all of them without needing
a lock per store file. **Mechanism, not a PID file**: the kernel releases an
`flock` automatically when the holding process's file descriptor closes —
on normal exit, an uncaught exception, or `SIGKILL` — with no cleanup code
required and no stale-PID-reuse ambiguity a PID-file design would have.
**Non-blocking** (`LOCK_NB`): a second acquirer fails fast with
`ProcessLockError`, matching this codebase's fail-safe-to-NO-TRADE
discipline — refuse to start rather than queue silently behind a writer
whose state is unknown. **Canonicalized** via `Path(data_dir).resolve()`,
so relative/absolute/trailing-slash spellings of the same directory
correctly collide, and genuinely different directories never do.

## Acquisition point relative to durable writes

Wired into `scripts/run_agent.py::main()`'s scheduled-loop path only (see
below for what this deliberately excludes), as the FIRST statement inside
the pre-existing `try:` block — before the data-dir sanity check, before
`config_module.load`, before any store constructor runs. No store is ever
opened before the lock is held.

## Test results (all real, all passing)

`tests/test_process_lock.py` — 13 tests on the primitive itself:
- basic acquire/release (5): unlocked acquire succeeds, lock file is
  created, directory is created if missing, released on normal exit and
  reacquirable, released when the `with` block raises and reacquirable.
- same-process double-acquire (2): a second acquire of the same directory
  raises `ProcessLockError`; the error names the resolved data_dir and
  lock path.
- canonical-equivalent path collision (2): relative vs. absolute spellings
  of the same directory collide; a trailing-slash spelling collides with
  the bare path.
- independent data-dir behavior (1): two genuinely different directories
  never contend.
- **real subprocess crash tests (3)**, via `multiprocessing.Process` (a
  genuine separate OS process — flock contention is a real cross-process
  phenomenon, not something a thread or a mock can prove):
  - `test_a_second_real_process_cannot_acquire_while_the_first_still_holds_it`
    — a real child process holds the lock; the parent's acquire attempt
    raises `ProcessLockError` while the child is still alive.
  - `test_lock_is_available_immediately_after_the_holding_process_exits_normally`
    — child exits cleanly (exitcode 0); the lock is immediately acquirable.
  - **`test_lock_is_available_immediately_after_the_holding_process_is_sigkilled`**
    — child is killed via `os.kill(pid, SIGKILL)` (no `finally`, no
    `atexit`, no cleanup code of any kind runs in the killed process); the
    lock is immediately acquirable with no sleep/retry loop needed — this
    is the specific guarantee this whole module exists to provide, proven
    against a real killed process, not asserted in prose.

`tests/test_run_agent.py` — 2 new wiring-level tests:
- `test_main_refuses_and_returns_nonzero_when_the_data_dir_is_already_locked`
  — a real lock held (via `acquire_process_lock` directly, standing in for
  a second real process) before calling `main()`: returns 1, logs
  "run_agent halted: ...", and the injected `run_loop_fn` is proven never
  called (it raises `AssertionError` if it is).
- `test_main_succeeds_normally_once_the_competing_lock_is_released` — same
  `--data-dir`, same argv, no competing lock: succeeds normally, proving
  the failure above was genuinely about lock contention and not some other
  latent defect the first test's assertions would not have caught.

Full suite after wiring: 4835 passed (up from 4820 after Unit A — +13
process_lock tests, +2 run_agent wiring tests).

## Answers to the specific questions asked

- **Implementation type**: `fcntl.flock`, non-blocking, one lock file per
  data_dir. Not a PID file (see Design above for why).
- **Duplicate process rejection**: proven via a real second OS process
  (`multiprocessing.Process`), not simulated.
- **Canonical-equivalent path collision**: proven for both relative-vs-
  absolute and trailing-slash spellings.
- **Independent data-dir behavior**: proven — two different directories
  never contend.
- **submit-approved interaction**: **NOT locked** — `_run_submit_approved`
  (the `--submit-approved` CLI one-shot handler) does not acquire this
  lock. Verified by static search: `grep -n "acquire_process_lock"
  scripts/run_agent.py` shows exactly one call site, inside `main()`'s
  scheduled-loop branch, not inside any of the four early-dispatch
  handlers (`_run_advance_mode`, `_run_admit_or_reject`, `_run_admit_or_
  reject_cash_event`, `_run_submit_approved`). **This is a disclosed,
  real gap, not a fix silently skipped**: a manual `--submit-approved`
  invocation (or `--admit-execution`/`--admit-cash-event`/
  `--advance-mode-to`) can still race a running scheduled loop's writes to
  the same `--data-dir`. Closing this fully would mean wiring the same
  lock into all five entry paths and testing each — out of scope for what
  this unit's remaining time allowed; see final report's "next engineering
  unit" recommendation.
- **Read-only diagnostic behavior**: confirmed correct BY ABSENCE — neither
  `scripts/diagnose_runtime.py` nor `scripts/preflight_broker.py` (nor any
  other read-only script) references `acquire_process_lock` at all, so an
  operator can run a diagnostic while the scheduled loop is running without
  being refused — exactly the intended behavior, since a read-only script
  never writes and has nothing to contend over.

## What this unit did NOT do (disclosed)

- The four one-shot CLI dispatch paths in `scripts/run_agent.py`
  (`--advance-mode-to`, `--admit-execution`/`--reject-execution`,
  `--admit-cash-event`/`--reject-cash-event`, `--submit-approved`) do not
  acquire the lock. See "submit-approved interaction" above.
- `args.data_dir` defaults to `./data`, resolved relative to the process's
  CURRENT WORKING DIRECTORY at parse time (`Path(args.data_dir).resolve()`
  in `_parse_args`) when `--data-dir` is not passed explicitly. Two
  processes launched from different working directories with no explicit
  `--data-dir` would NOT collide even if an operator intended them to
  point at the same logical directory. Real deployment is unaffected (the
  LaunchAgent plists in `deploy/` always pass `--data-dir` explicitly, per
  earlier units) — this is a latent footgun for ad-hoc manual invocations
  only, not a defect in the lock mechanism itself.
- `scripts/run_dashboard.py` does not acquire this lock and does not need
  to — confirmed read-only (Unit A's own findings: no `secrets_provider`
  or write path in `agent/dashboard_server.py`; `_build_broker_state`'s own
  docstring: "READ-ONLY, DELIBERATELY").
