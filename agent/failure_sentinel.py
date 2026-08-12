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
it on every save is the right choice, not an oversight.

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
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from pathlib import Path


@dataclass(frozen=True)
class FailureRecord:
    exc_type: str
    message: str
    first_at: datetime
    last_at: datetime
    consecutive_count: int


def record_failure(prior: FailureRecord | None, *, exc_type: str, message: str,
                   now: datetime) -> FailureRecord:
    """The same `exc_type` as `prior` extends the streak (increments
    `consecutive_count`, keeps `first_at`, updates `message` to this
    occurrence's) -- regardless of whether `message` itself matches;
    anything else -- a different `exc_type`, or no prior record at all --
    starts a fresh one at count 1. See module docstring for why exception
    type, not message text, is the recurrence key: `type(exc).__name__` is
    exactly what scripts/run_agent.py already has in hand alongside
    `str(exc)`, and it is stable across restarts in a way an interpolated
    message (a timestamp, a request id, a dollar figure) is not."""
    if prior is not None and prior.exc_type == exc_type:
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
    p = Path(path)
    if not p.exists():
        return None
    d = json.loads(p.read_text())
    return FailureRecord(
        exc_type=d["exc_type"],
        message=d["message"],
        first_at=datetime.fromisoformat(d["first_at"]),
        last_at=datetime.fromisoformat(d["last_at"]),
        consecutive_count=d["consecutive_count"],
    )


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
    install) it's most likely to be needed."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    d = asdict(record)
    d["first_at"] = record.first_at.isoformat()
    d["last_at"] = record.last_at.isoformat()
    p.write_text(json.dumps(d))


def clear(path: str | Path) -> None:
    """Deletes the sentinel file (notification-noise unit, 2026-08-12): the
    RECOVERY half of the same mechanism -- once a process resumes
    succeeding, the incident this file was tracking is over, and the next
    failure (of any type) must start a fresh streak at count 1, not
    silently continue the old one. A safe no-op if the file does not exist
    (nothing to clear -- e.g. a process that has never failed)."""
    p = Path(path)
    p.unlink(missing_ok=True)
