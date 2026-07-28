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
it on every save is the right choice, not an oversight."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from pathlib import Path


@dataclass(frozen=True)
class FailureRecord:
    message: str
    first_at: datetime
    last_at: datetime
    consecutive_count: int


def record_failure(prior: FailureRecord | None, *, message: str,
                   now: datetime) -> FailureRecord:
    """The same `message` as `prior` extends the streak (increments
    `consecutive_count`, keeps `first_at`); anything else -- including no
    prior record at all -- starts a fresh one at count 1. Comparing the
    raw exception message string is a deliberately simple signal: it is
    exactly what scripts/run_agent.py already has in hand (`str(exc)`), and
    a genuinely stuck failure (the same locked keychain, the same expired
    credential, the same reconciliation mismatch) reliably reproduces the
    same message on every relaunch, while an unrelated failure reliably
    does not."""
    if prior is not None and prior.message == message:
        return replace(prior, last_at=now, consecutive_count=prior.consecutive_count + 1)
    return FailureRecord(message=message, first_at=now, last_at=now, consecutive_count=1)


def should_alert(record: FailureRecord, *, threshold: int = 3) -> bool:
    """True once the SAME failure has recurred at least `threshold` times
    in a row. A single occurrence of a new failure never alerts -- only a
    failure that keeps recurring identically does, which is what
    distinguishes "permanent" from "transient" here."""
    return record.consecutive_count >= threshold


def load(path: str | Path) -> FailureRecord | None:
    p = Path(path)
    if not p.exists():
        return None
    d = json.loads(p.read_text())
    return FailureRecord(
        message=d["message"],
        first_at=datetime.fromisoformat(d["first_at"]),
        last_at=datetime.fromisoformat(d["last_at"]),
        consecutive_count=d["consecutive_count"],
    )


def save(path: str | Path, record: FailureRecord) -> None:
    """Overwrites whatever was there -- see module docstring for why this
    one, unlike this codebase's other durable stores, is NOT append-only."""
    d = asdict(record)
    d["first_at"] = record.first_at.isoformat()
    d["last_at"] = record.last_at.isoformat()
    Path(path).write_text(json.dumps(d))
