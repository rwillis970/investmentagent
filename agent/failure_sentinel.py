"""Answering, concretely, "how does an operator find out" that scripts/
run_agent.py is stuck in a PERMANENT failure -- a locked keychain, an
expired credential, a genuine reconciliation halt -- rather than silently
restart-looping forever with nobody watching (§11, final unit).

`deploy/com.investmentagent.reconcile-loop.plist`'s KeepAlive + 60s
ThrottleInterval already means launchd re-runs `main()`'s own except-block
on every relaunch after a crash -- no separate watchdog PROCESS is needed;
main() itself sees every consecutive failure, one call at a time, each
time it is relaunched. This module is the pure decision logic sitting
behind that: does the CURRENT failure look like the SAME one as last time
(a stuck, permanent condition worth alerting on), or a DIFFERENT one (a
fresh problem, not yet worth alerting on its first occurrence alone, since
a single transient network blip must not page anyone)?

DELIBERATELY NOT append-only, unlike every other durable store in this
codebase (`ModeStore`/`LedgerStore`/`AuditLog`). This is disposable
operational state -- what failed last time, and how many times in a row --
not evidence anything downstream needs a permanent history of; overwriting
it on every save is the right choice, not an oversight. The permanent
record of what actually happened, when, lives in `AuditLog` (durable,
hash-chained, append-only) -- this file is never that, and nothing here
ever removes or edits an audit row.

ACTIVE VS. RECOVERED (overnight-hardening unit, 2026-08-13). Real defect
found running this on the real Mac: a `DataDirConflict` (since fixed -- see
`scripts/run_agent.py::_check_data_dir_sanity`'s own archived-sibling
exemption) wrote a sentinel record, and then the process outside a trading
session for hours afterward -- `agent.run_loop.run_loop` correctly never
calls `run_cycle` outside a session, so `on_cycle_success` (the ONLY thing
that used to clear this file, via `clear()`) never fired. The process was
genuinely healthy; the sentinel had no way to say so. `FailureRecord` now
carries `status` (`"active"` or `"recovered"`) and `recovered_at`
(`None` while active). `mark_recovered` flips `status` without discarding
`exc_type`/`message`/`first_at`/`last_at`/`consecutive_count` -- an operator
(or the dashboard) can still see exactly what the LAST failure was and when
it cleared, not just that nothing is wrong right now. `record_failure` only
extends a streak when the prior record is still ACTIVE and matches
`exc_type`; a new failure arriving after a recovery -- even the identical
exception type -- starts a fresh streak at 1, never silently reattaching to
an incident that was already closed out. `clear()` (delete the file
outright) still exists for a caller that genuinely wants "nothing on
record" rather than "the last thing on record was resolved" -- but the two
call sites that used to call it (`scripts/run_agent.py`'s cycle-success
hook, and the new read-only diagnostic's own recovery marking -- see
`agent/diagnostics.py`) now call `mark_recovered` instead, specifically so
the dashboard and `data/runtime_status.json` have something to show besides
a missing file. BACKWARD COMPATIBLE: `load()` on an old-format file with no
`status`/`recovered_at` keys defaults them to `"active"`/`None` -- an
untouched sentinel from before this change reads exactly as it always did,
an active failure, not a silently-recovered one.

`save` CREATES ITS OWN PARENT DIRECTORY (real gap found running the loop
for the first time): unlike `ModeStore`/`LedgerStore`/`AuditLog`, which
deliberately do NOT do this (an operator is expected to create `state/`
first; their own tests rely on a missing parent raising), this file's
entire purpose is making a permanent failure visible -- refusing to write
because the very `state/` directory a fresh install hasn't created yet
doesn't exist would silently disable the one mechanism meant to surface
exactly this kind of problem, at exactly the moment it's most likely to be
needed. See `save`'s own docstring.

KEYED ON EXCEPTION TYPE, NOT MESSAGE TEXT. This originally compared the raw
exception message string, which was wrong: a genuinely permanent failure
whose message carries incidental, ever-changing detail -- a timestamp, a
request id, the cash figure in a reconciliation mismatch -- never
reproduces the same string twice, so it never looked like a recurrence at
all and would restart-loop forever without ever notifying. `type(exc).
__name__` is what this codebase's call sites actually vary meaningfully
by (`SecretNotFoundError`, `TransportError`, `ReconciliationError`, ...);
the same exception TYPE recurring, whatever its message's incidental
details, is "the same permanent failure" in every sense that matters here.
`message` is still recorded and updated to the latest occurrence (useful
for an operator to see exactly what's happening right now), but it is no
longer part of what determines recurrence."""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from pathlib import Path


ACTIVE = "active"
RECOVERED = "recovered"


@dataclass(frozen=True)
class FailureRecord:
    exc_type: str
    message: str
    first_at: datetime
    last_at: datetime
    consecutive_count: int
    # overnight-hardening unit, 2026-08-13 -- see module docstring's ACTIVE
    # VS. RECOVERED section. Defaults preserve every existing call site
    # (tests, scripts/run_agent.py's own construction sites) that built a
    # FailureRecord before these fields existed.
    status: str = ACTIVE
    recovered_at: datetime | None = None
    # PAUSED-reconcile-follow-up runtime-status unit, 2026-08-14. WHICH of
    # this codebase's three recovery producers actually cleared this
    # incident -- "cycle" (agent.run_loop.run_cycle succeeded inside a real
    # scheduled market-session cycle -- the strongest possible evidence),
    # "diagnostic" (agent.diagnostics.diagnose_account's own read-only,
    # after-hours-safe check found every component PASS/WARN), or
    # "reconcile_once" (scripts.run_agent._run_reconcile_once actually
    # exercised the SAME broker-read-plus-exact-reconciliation code path a
    # real cycle does -- see that function's own docstring for why this is
    # legitimate evidence, distinct from but not weaker than "diagnostic").
    # `None` for an active record, or for a record recovered before this
    # field existed (backward compatible, same convention as `status`/
    # `recovered_at` above -- an old-format recovered record simply doesn't
    # say which producer did it, which is exactly what `None` already meant
    # for those two fields before they existed at all).
    recovered_by: str | None = None


def record_failure(prior: FailureRecord | None, *, exc_type: str, message: str,
                   now: datetime) -> FailureRecord:
    """The same `exc_type` as an ACTIVE `prior` extends the streak
    (increments `consecutive_count`, keeps `first_at`, updates `message` to
    this occurrence's) -- regardless of whether `message` itself matches;
    anything else -- a different `exc_type`, no prior record at all, OR a
    prior record that is already RECOVERED -- starts a fresh one at count 1.
    See module docstring for why exception type, not message text, is the
    recurrence key: `type(exc).__name__` is exactly what scripts/
    run_agent.py already has in hand alongside `str(exc)`, and it is stable
    across restarts in a way an interpolated message (a timestamp, a
    request id, a dollar figure) is not.

    THE RECOVERED CHECK (overnight-hardening unit, 2026-08-13) is what makes
    "new failure after recovery becomes active again, fresh streak" true: a
    prior record whose `status` is already RECOVERED represents a CLOSED
    incident -- a new failure of the identical exc_type arriving later is a
    new incident that happens to look the same, not a continuation of one
    an operator already got a recovery notification for. Without this
    check, `should_alert`'s own `== threshold` logic (fires exactly once
    per crossing) would never fire again for a repeat-offender exc_type,
    because the reattached streak would already be past `threshold` from
    the first incident."""
    if prior is not None and prior.status == ACTIVE and prior.exc_type == exc_type:
        return replace(prior, message=message, last_at=now,
                      consecutive_count=prior.consecutive_count + 1)
    return FailureRecord(exc_type=exc_type, message=message, first_at=now,
                        last_at=now, consecutive_count=1)


def should_alert(record: FailureRecord, *, threshold: int = 3,
                 escalation_counts: tuple[int, ...] = (5, 25, 100)) -> bool:
    """True at the exact instant the SAME failure crosses `threshold`
    consecutive occurrences, and again at each of `escalation_counts` --
    never on every occurrence in between (notification-noise unit,
    2026-08-12).

    THE BUG THIS REPLACES: the original `consecutive_count >= threshold`
    is true on EVERY call once the streak passes `threshold` -- since
    `scripts/run_agent.py`'s `main()` calls this (and fires a real macOS
    notification when it returns True) on every single launchd relaunch,
    one persistent incident produced one notification per relaunch,
    forever, for as long as the underlying condition stayed broken (a real
    deployment hit 205 in a row for a single incident). A single occurrence
    of a NEW failure still never alerts -- only a failure that keeps
    recurring identically does, which is what distinguishes "permanent"
    from "transient" here; this function now ALSO never re-alerts for the
    same reason a single occurrence doesn't: an operator who has already
    been told "this has failed 3 times in a row" gains nothing from being
    told again at 4, 6, 7, ... 204 -- only at meaningfully escalated
    milestones (5, 25, 100 by default) does the SAME information become
    worth a second interruption."""
    return record.consecutive_count == threshold or record.consecutive_count in escalation_counts


def load(path: str | Path) -> FailureRecord | None:
    """BACKWARD COMPATIBLE (overnight-hardening unit, 2026-08-13): a file
    written before `status`/`recovered_at` existed has neither key --
    `.get(..., ACTIVE)`/`.get(..., None)` read those as "active, never
    recovered," exactly what an old-format file always meant before this
    change, not a silently-invented recovery."""
    p = Path(path)
    if not p.exists():
        return None
    d = json.loads(p.read_text())
    recovered_at = d.get("recovered_at")
    return FailureRecord(
        exc_type=d["exc_type"],
        message=d["message"],
        first_at=datetime.fromisoformat(d["first_at"]),
        last_at=datetime.fromisoformat(d["last_at"]),
        consecutive_count=d["consecutive_count"],
        status=d.get("status", ACTIVE),
        recovered_at=datetime.fromisoformat(recovered_at) if recovered_at else None,
        recovered_by=d.get("recovered_by"),
    )


def mark_recovered(path: str | Path, *, now: datetime,
                   recovered_by: str | None = None) -> FailureRecord | None:
    """The RECOVERY half (overnight-hardening unit, 2026-08-13; see module
    docstring's ACTIVE VS. RECOVERED section) -- replaces `clear()` at all
    three of this codebase's real call sites (`scripts/run_agent.py`'s
    cycle-success hook, `agent/diagnostics.py`'s own all-PASS path, and
    `scripts/run_agent.py`'s `--reconcile-once` success path -- PAUSED-
    reconcile-follow-up runtime-status unit, 2026-08-14). Returns `None`,
    and writes nothing, if there is no sentinel to recover FROM (a process
    that has never failed) -- a safe no-op, matching `clear()`'s own
    no-op-if-missing behaviour. If the loaded record is already RECOVERED,
    this is idempotent: `recovered_at`/`recovered_by` are NOT overwritten a
    second time, preserving the actual moment (and producer) recovery
    happened rather than whichever producer last re-observed it.

    `recovered_by` (PAUSED-reconcile-follow-up runtime-status unit,
    2026-08-14): which producer is recovering this incident -- see
    `FailureRecord.recovered_by`'s own docstring for the three real values.
    `None` (the default) is still accepted for a caller that genuinely
    doesn't know or doesn't care -- no existing call site is broken by this
    parameter's addition."""
    prior = load(path)
    if prior is None:
        return None
    if prior.status == RECOVERED:
        return prior
    record = replace(prior, status=RECOVERED, recovered_at=now, recovered_by=recovered_by)
    save(path, record)
    return record


def save(path: str | Path, record: FailureRecord) -> None:
    """Overwrites whatever was there -- see module docstring for why this
    one, unlike this codebase's other durable stores, is NOT append-only.

    Creates its own parent directory if missing (real gap found running the
    loop for the first time): ModeStore/LedgerStore/AuditLog deliberately do
    NOT do this -- an operator is expected to create state/ before those are
    ever written to, and their own tests rely on a missing parent raising.
    This file is different: its entire purpose is making a permanent
    failure visible, and refusing to write because the very state/
    directory that a fresh install hasn't created yet doesn't exist is
    self-defeating -- it silently disables the one mechanism meant to
    surface exactly this kind of problem, at exactly the moment (a fresh
    install) it's most likely to be needed.

    ATOMIC WRITE (overnight-hardening unit, 2026-08-13): write-to-temp-then-
    `os.replace` -- same technique as `agent/runtime_status.py`'s own
    atomic write, applied here too, since this file is read by an operator
    (and, from tonight, the read-only diagnostic) precisely at the moments
    a process might be crashing -- exactly when a plain truncating write is
    most likely to be interrupted mid-write and leave behind unparseable
    JSON, defeating the one file whose entire job is being readable during
    a failure."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    d = asdict(record)
    d["first_at"] = record.first_at.isoformat()
    d["last_at"] = record.last_at.isoformat()
    d["recovered_at"] = record.recovered_at.isoformat() if record.recovered_at else None
    tmp = p.with_suffix(p.suffix + f".tmp-{os.getpid()}")
    tmp.write_text(json.dumps(d))
    os.replace(tmp, p)


def clear(path: str | Path) -> None:
    """Deletes the sentinel file (notification-noise unit, 2026-08-12): the
    RECOVERY half of the same mechanism -- once a process resumes
    succeeding, the incident this file was tracking is over, and the next
    failure (of any type) must start a fresh streak at count 1, not
    silently continue the old one. A safe no-op if the file does not exist
    (nothing to clear -- e.g. a process that has never failed)."""
    p = Path(path)
    p.unlink(missing_ok=True)
