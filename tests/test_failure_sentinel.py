"""agent/failure_sentinel.py -- answering, concretely, "how does an operator
find out" that a launchd-restarted process is stuck in a PERMANENT failure
(a locked keychain, an expired credential, a genuine reconciliation halt),
not silently restart-looping forever with nobody watching.

launchd's own ThrottleInterval already re-runs scripts/run_agent.py's
except-block on every relaunch attempt after a crash (deploy/
com.investmentagent.reconcile-loop.plist: KeepAlive + a 60s throttle) -- so
main() itself sees every consecutive failure, one call at a time, with no
separate watchdog process needed. This module is the pure decision logic:
does the CURRENT failure look like the SAME one as last time (a stuck,
permanent condition), or a DIFFERENT one (a fresh, possibly-transient
problem, worth NOT alerting on the first occurrence alone)?

KEYED ON EXCEPTION TYPE, NOT MESSAGE TEXT. Originally this compared raw
message strings, which meant a genuinely permanent failure whose message
varies between restarts -- a timestamp, a request id, the cash figure in a
reconciliation mismatch -- never looked like a recurrence at all: every
occurrence reset the counter to 1, and it restart-looped forever without
ever notifying. `type(exc).__name__` is what this codebase's call sites
actually vary meaningfully by (SecretNotFoundError, TransportError,
ReconciliationError, ...) -- the same TYPE of problem recurring, however
its message's incidental details drift, is "the same permanent failure" in
every sense that matters here. The message is still recorded (updated to
the latest occurrence, for an operator's benefit), but no longer part of
what determines recurrence.

DELIBERATELY NOT append-only, unlike every other durable store in this
codebase (ModeStore/LedgerStore/AuditLog). This is disposable operational
state -- "what failed last time, and how many times in a row" -- not
evidence anything downstream needs a permanent history of. Overwriting it
on every save is the right choice here, not an oversight."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from agent.failure_sentinel import (FailureRecord, load, record_failure,
                                    save, should_alert)

T0 = datetime(2026, 7, 20, 15, 0, tzinfo=timezone.utc)


# -------------------------------------------------------------- record_failure

def test_a_first_ever_failure_starts_a_new_record_at_count_one():
    rec = record_failure(None, exc_type="RuntimeError", message="boom", now=T0)
    assert rec.exc_type == "RuntimeError"
    assert rec.message == "boom"
    assert rec.first_at == T0
    assert rec.last_at == T0
    assert rec.consecutive_count == 1


def test_the_same_exception_type_recurring_increments_the_count():
    rec = record_failure(None, exc_type="RuntimeError", message="boom", now=T0)
    rec = record_failure(rec, exc_type="RuntimeError", message="boom",
                         now=T0 + timedelta(minutes=1))
    rec = record_failure(rec, exc_type="RuntimeError", message="boom",
                         now=T0 + timedelta(minutes=2))
    assert rec.consecutive_count == 3
    assert rec.first_at == T0                       # unchanged across recurrences
    assert rec.last_at == T0 + timedelta(minutes=2)  # tracks the most recent


def test_the_same_exception_type_recurring_with_a_varying_message_still_increments():
    """The actual bug being fixed: a permanent failure (e.g. a
    reconciliation halt) whose message carries incidental, ever-changing
    detail -- a timestamp, a request id, the cash figure in the mismatch --
    must still be recognized as the SAME recurring failure, because it is
    the same exception type every time."""
    rec = record_failure(None, exc_type="ReconciliationError",
                         message="settled_cash mismatch: local=500.00 broker=498.13",
                         now=T0)
    rec = record_failure(rec, exc_type="ReconciliationError",
                         message="settled_cash mismatch: local=500.00 broker=501.77",
                         now=T0 + timedelta(minutes=1))
    rec = record_failure(rec, exc_type="ReconciliationError",
                         message="settled_cash mismatch: local=500.00 broker=496.02",
                         now=T0 + timedelta(minutes=2))
    assert rec.consecutive_count == 3
    assert rec.first_at == T0
    # message is updated to the latest occurrence -- useful for an operator,
    # but (see above) no longer part of what determines recurrence.
    assert "496.02" in rec.message
    assert should_alert(rec, threshold=3) is True


def test_a_different_exception_type_resets_the_count_to_one_even_with_the_same_message():
    rec = record_failure(None, exc_type="RuntimeError", message="boom", now=T0)
    rec = record_failure(rec, exc_type="RuntimeError", message="boom",
                         now=T0 + timedelta(minutes=1))
    assert rec.consecutive_count == 2
    rec = record_failure(rec, exc_type="ValueError", message="boom",
                         now=T0 + timedelta(minutes=2))
    assert rec.consecutive_count == 1
    assert rec.exc_type == "ValueError"
    assert rec.first_at == T0 + timedelta(minutes=2)


# ---------------------------------------------------------------- should_alert

def test_should_not_alert_below_the_threshold():
    rec = FailureRecord(exc_type="RuntimeError", message="boom", first_at=T0,
                       last_at=T0, consecutive_count=2)
    assert should_alert(rec, threshold=3) is False


def test_should_alert_at_or_above_the_threshold():
    rec = FailureRecord(exc_type="RuntimeError", message="boom", first_at=T0,
                       last_at=T0, consecutive_count=3)
    assert should_alert(rec, threshold=3) is True
    rec2 = FailureRecord(exc_type="RuntimeError", message="boom", first_at=T0,
                        last_at=T0, consecutive_count=10)
    assert should_alert(rec2, threshold=3) is True


def test_a_single_transient_failure_does_not_alert():
    """The whole point: one occurrence of a new, possibly-transient failure
    must not immediately alert -- only a failure that keeps recurring
    identically does."""
    rec = record_failure(None, exc_type="TransportError", message="network blip", now=T0)
    assert should_alert(rec, threshold=3) is False


# --------------------------------------------------------------- load / save

def test_load_of_a_nonexistent_path_returns_none(tmp_path):
    assert load(tmp_path / "nope.json") is None


def test_save_then_load_round_trips(tmp_path):
    path = tmp_path / "failure_sentinel.json"
    rec = FailureRecord(exc_type="RuntimeError", message="boom", first_at=T0,
                       last_at=T0 + timedelta(minutes=5), consecutive_count=4)
    save(path, rec)
    loaded = load(path)
    assert loaded == rec


def test_save_creates_its_own_parent_directory(tmp_path):
    """Real gap found running the loop for the first time: on a fresh
    install, state/ does not exist yet, so the mechanism whose entire job is
    surfacing a permanent failure could not even record one -- save() would
    raise FileNotFoundError, silently defeating the whole point. save()
    must create whatever directory it needs, the same way an operator would
    have to manually for ModeStore/LedgerStore/AuditLog (which deliberately
    do NOT do this -- see their own tests) -- but this file is different:
    it exists purely to make a failure visible, and refusing to write
    because of the very kind of fresh-install gap it's supposed to help
    diagnose is self-defeating in a way those other stores' fail-loud
    posture is not."""
    path = tmp_path / "does-not-exist-yet" / "nested" / "failure_sentinel.json"
    rec = FailureRecord(exc_type="RuntimeError", message="boom", first_at=T0,
                       last_at=T0, consecutive_count=1)
    save(path, rec)   # must not raise
    assert path.exists()
    assert load(path) == rec


def test_save_overwrites_rather_than_appending(tmp_path):
    """Deliberately NOT append-only, unlike ModeStore/LedgerStore/AuditLog --
    see module docstring for why this one is different."""
    path = tmp_path / "failure_sentinel.json"
    save(path, FailureRecord(exc_type="RuntimeError", message="first", first_at=T0,
                            last_at=T0, consecutive_count=1))
    save(path, FailureRecord(exc_type="RuntimeError", message="second", first_at=T0,
                            last_at=T0, consecutive_count=2))
    loaded = load(path)
    assert loaded.message == "second"
    # exactly one JSON object on disk, not two lines appended
    assert path.read_text().count("{") == 1


def test_a_realistic_load_record_save_cycle_across_three_relaunches(tmp_path):
    """Simulates what scripts/run_agent.py actually does on each of three
    consecutive launchd relaunches of a permanently-failing process."""
    path = tmp_path / "failure_sentinel.json"
    for i in range(3):
        prior = load(path)
        rec = record_failure(prior, exc_type="SecretNotFoundError",
                             message="SecretNotFoundError: keychain locked",
                             now=T0 + timedelta(minutes=i))
        save(path, rec)
    final = load(path)
    assert final.consecutive_count == 3
    assert should_alert(final, threshold=3) is True


def test_a_realistic_reconciliation_halt_with_a_drifting_message_still_alerts(tmp_path):
    """End-to-end version of the message-drift regression, through the
    actual load/save cycle a real deployment goes through."""
    path = tmp_path / "failure_sentinel.json"
    cash_figures = ["498.13", "501.77", "496.02"]
    for i, cash in enumerate(cash_figures):
        prior = load(path)
        rec = record_failure(prior, exc_type="ReconciliationError",
                             message=f"settled_cash mismatch: broker={cash}",
                             now=T0 + timedelta(minutes=i))
        save(path, rec)
    final = load(path)
    assert final.consecutive_count == 3
    assert should_alert(final, threshold=3) is True
