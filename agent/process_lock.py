"""Process lock (Unit B, reconstructed 2026-08-13).

THE DEFECT THIS CLOSES. Nothing in this codebase has ever prevented two
`scripts/run_agent.py` processes from being started against the SAME
`--data-dir` at once (e.g. a LaunchAgent restart racing a manually-started
debug run, or an operator forgetting a prior process is still running).
Every durable store here (`agent.ledger_store.LedgerStore`,
`agent.mode_store.ModeStore`, `agent.audit.AuditLog`, `agent.
execution_quarantine.ExecutionQuarantineStore`, `agent.cash_event_
quarantine.CashEventQuarantineStore`) is a plain append-only JSONL file
with no cross-process write coordination of its own -- two processes
interleaving writes to the same file is a real corruption risk this module
exists to prevent, structurally, before any store is ever opened.

THE MECHANISM: `fcntl.flock(fd, LOCK_EX | LOCK_NB)` on a sentinel file
(`<data_dir>/.agent.lock`), never a PID file. A PID file requires the
LOCK-ER to remember to delete it on exit and requires every LOCK-ee to
independently guess whether a PID that still exists in `/proc` is actually
the same process or a PID number that has since been reused by something
else entirely -- both are real, well-known failure modes. `flock` has
neither: the kernel releases the lock automatically when the holding
process's file descriptor closes, for ANY reason -- normal exit, an
uncaught exception, or `SIGKILL` (which a Python `finally` block cannot run
under at all) -- so a crashed or killed process releases the lock
immediately, atomically, with no cleanup code required and no stale-PID
ambiguity possible. `LOCK_NB` (non-blocking) means a second process trying
to acquire an already-held lock fails FAST with `BlockingIOError`, never
hangs waiting -- this codebase's own fail-safe-to-NO-TRADE discipline
extends here: a caller unable to get the lock must refuse to start, not
queue up silently behind another writer it cannot see the state of.

CANONICALIZATION. The lock file's path is derived from `Path(data_dir).
resolve()` -- two different string spellings of the same directory
(relative vs. absolute, a trailing slash, `..` segments) resolve to the
SAME lock file and correctly contend with each other. Two GENUINELY
different data directories never contend, by construction (each gets its
own `.agent.lock` file, inside itself).

WHAT THIS MODULE DOES NOT DO (disclosed, not fixed here -- see
docs/unit_b_process_lock.md for the full audit). `scripts/run_agent.py`'s
one-shot CLI paths (`--admit-execution`/`--reject-execution`/
`--admit-cash-event`/`--reject-cash-event`/`--submit-approved`/
`--advance-mode-to`) do NOT acquire this lock as of this unit -- only the
main scheduled-loop path does. A manual CLI action while the scheduled
loop is running can therefore still race it. This is a real, disclosed gap,
not a silent one."""
from __future__ import annotations

import contextlib
import fcntl
from pathlib import Path
from typing import Iterator

LOCK_FILENAME = ".agent.lock"


class ProcessLockError(Exception):
    """Raised when `acquire_process_lock` cannot get an exclusive lock on
    `data_dir` -- almost always because another process already holds it.
    Carries the resolved data_dir and lock file path (never a secret, never
    anything from the competing process itself, which this module has no
    way to identify anyway -- flock does not expose the holder's PID)."""

    def __init__(self, *, data_dir: Path, lock_path: Path):
        self.data_dir = data_dir
        self.lock_path = lock_path
        super().__init__(
            f"could not acquire exclusive process lock for data_dir="
            f"{str(data_dir)!r} (lock file {str(lock_path)!r}) -- another "
            f"process already holds it, or holds a different directory "
            f"that resolves to the same path"
        )


@contextlib.contextmanager
def acquire_process_lock(data_dir: str | Path) -> Iterator[None]:
    """Exclusive, non-blocking process lock scoped to `data_dir`. Raises
    `ProcessLockError` immediately if another process already holds it --
    never blocks, never retries. Creates `data_dir` if it does not already
    exist (mirrors every store in this codebase's own "the directory is
    allowed not to exist yet" posture -- see e.g. agent.ledger_store.
    LedgerStore). Releases automatically on any exit from the `with` block
    -- normal return, raised exception, or (structurally, via the OS, not
    this function's own code, which cannot run under SIGKILL at all) the
    process being killed outright."""
    resolved = Path(data_dir).resolve()
    resolved.mkdir(parents=True, exist_ok=True)
    lock_path = resolved / LOCK_FILENAME
    fh = open(lock_path, "w")
    try:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError) as exc:
            raise ProcessLockError(data_dir=resolved, lock_path=lock_path) from None
        yield
    finally:
        # Releases the flock as a side effect of closing the fd -- this is
        # the NORMAL-EXIT and RAISED-EXCEPTION release path. The SIGKILL
        # release path is the kernel's, not this line's -- see module
        # docstring.
        fh.close()
