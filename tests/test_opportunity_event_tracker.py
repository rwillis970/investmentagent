"""agent/opportunity_event_tracker.py (unattended wiring unit, 2026-08-01):
durable "already handled" tracker closing the gap agent.materiality_cycle's
own module docstring names -- "there is no 'already analysed this filing'
tracker in this codebase yet." Own file, append-only, replay-on-load,
matching this codebase's established store pattern.

IDENTITY = event_id. See agent/run_loop.py's own module docstring for the
full justification: for a FILING-typed OpportunityEvent, event_id already
encodes (source_id, symbol, the filing's own observed_at) via agent.
materiality_cycle.run_materiality_cycle's own construction -- deterministic
and stable across cycles for "the same" filing, and naturally different the
moment a genuinely newer filing supersedes it (a new observed_at). No
separate (symbol, accession_number) derivation is needed.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from agent.opportunity_event_tracker import (HandledRecord,
                                             OpportunityEventTracker,
                                             OpportunityEventTrackerError)

T0 = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
T1 = datetime(2026, 8, 1, 12, 5, tzinfo=timezone.utc)
NEXT_SESSION_OPEN = datetime(2026, 8, 3, 13, 30, tzinfo=timezone.utc)   # next trading day's open


def store(tmp_path, name="opportunity_event_tracker.jsonl"):
    return OpportunityEventTracker(tmp_path / name)


def test_an_unmarked_event_is_not_handled(tmp_path):
    t = store(tmp_path)
    assert t.is_handled("sec_edgar:AAPL:2026-07-30T09:00:00+00:00", T0) is False


def test_mark_handled_makes_is_handled_true(tmp_path):
    t = store(tmp_path)
    t.mark_handled("e1", outcome="analyzed", now=T0)
    assert t.is_handled("e1", T0) is True


def test_mark_handled_returns_a_handled_record(tmp_path):
    t = store(tmp_path)
    rec = t.mark_handled("e1", outcome="refused", now=T0)
    assert isinstance(rec, HandledRecord)
    assert rec.event_id == "e1"
    assert rec.outcome == "refused"
    assert rec.handled_at == T0
    assert rec.eligible_again_at is None


def test_a_different_event_id_is_independently_unhandled(tmp_path):
    t = store(tmp_path)
    t.mark_handled("e1", outcome="analyzed", now=T0)
    assert t.is_handled("e2", T0) is False


def test_marking_the_same_event_id_twice_is_not_an_error(tmp_path):
    # Defensive, not expected in the real glue (which checks is_handled
    # first) -- but a second mark for the same event_id must not corrupt
    # state or raise; it simply records a second row.
    t = store(tmp_path)
    t.mark_handled("e1", outcome="analyzed", now=T0)
    t.mark_handled("e1", outcome="analyzed", now=T1)
    assert t.is_handled("e1", T1) is True
    assert len(t.all()) == 2


# ------------------------------------------------- eligible_again_at (earmarking unit)

def test_permanent_outcomes_are_handled_at_any_now(tmp_path):
    """'analyzed'/'refused' -- eligible_again_at=None means permanent."""
    t = store(tmp_path)
    t.mark_handled("e1", outcome="analyzed", now=T0)
    t.mark_handled("e2", outcome="refused", now=T0)
    far_future = datetime(2027, 1, 1, tzinfo=timezone.utc)
    assert t.is_handled("e1", far_future) is True
    assert t.is_handled("e2", far_future) is True


def test_a_temporary_outcome_is_handled_only_before_its_own_eligible_again_at(tmp_path):
    t = store(tmp_path)
    t.mark_handled("e1", outcome="budget_exceeded", now=T0,
                   eligible_again_at=NEXT_SESSION_OPEN)
    assert t.is_handled("e1", T0) is True
    assert t.is_handled("e1", NEXT_SESSION_OPEN - timedelta(seconds=1)) is True
    assert t.is_handled("e1", NEXT_SESSION_OPEN) is False
    assert t.is_handled("e1", NEXT_SESSION_OPEN + timedelta(days=1)) is False


def test_insufficient_settled_cash_is_also_a_temporary_outcome(tmp_path):
    t = store(tmp_path)
    t.mark_handled("e1", outcome="insufficient_settled_cash", now=T0,
                   eligible_again_at=NEXT_SESSION_OPEN)
    assert t.is_handled("e1", T0) is True
    assert t.is_handled("e1", NEXT_SESSION_OPEN) is False


def test_a_later_row_supersedes_an_earlier_ones_window(tmp_path):
    """is_handled consults the MOST RECENT row for a given event_id -- a
    fresh temporary record (re-screened, re-triggered, budget still tight)
    replaces the earlier one's own window rather than the earlier row
    remaining somehow authoritative."""
    t = store(tmp_path)
    t.mark_handled("e1", outcome="budget_exceeded", now=T0,
                   eligible_again_at=NEXT_SESSION_OPEN)
    later_window = NEXT_SESSION_OPEN + timedelta(days=1)
    t.mark_handled("e1", outcome="budget_exceeded", now=NEXT_SESSION_OPEN,
                   eligible_again_at=later_window)
    # Past the FIRST record's own window, but the SECOND (later) row is now
    # the one that governs -- still handled.
    assert t.is_handled("e1", NEXT_SESSION_OPEN + timedelta(hours=1)) is True
    assert t.is_handled("e1", later_window) is False


def test_a_permanent_row_after_a_temporary_one_makes_the_id_permanently_handled(tmp_path):
    t = store(tmp_path)
    t.mark_handled("e1", outcome="budget_exceeded", now=T0,
                   eligible_again_at=NEXT_SESSION_OPEN)
    t.mark_handled("e1", outcome="analyzed", now=NEXT_SESSION_OPEN)
    far_future = datetime(2027, 1, 1, tzinfo=timezone.utc)
    assert t.is_handled("e1", far_future) is True


# ----------------------------------------------------------------- durability

def test_handled_state_survives_a_reload(tmp_path):
    path = tmp_path / "tracker.jsonl"
    t = OpportunityEventTracker(path)
    t.mark_handled("e1", outcome="analyzed", now=T0)

    reloaded = OpportunityEventTracker(path)
    assert reloaded.is_handled("e1", T0) is True
    assert reloaded.is_handled("e2", T0) is False


def test_a_temporary_records_eligible_again_at_survives_a_reload(tmp_path):
    path = tmp_path / "tracker.jsonl"
    t = OpportunityEventTracker(path)
    t.mark_handled("e1", outcome="budget_exceeded", now=T0,
                   eligible_again_at=NEXT_SESSION_OPEN)

    reloaded = OpportunityEventTracker(path)
    assert reloaded.is_handled("e1", T0) is True
    assert reloaded.is_handled("e1", NEXT_SESSION_OPEN) is False


def test_a_reload_does_not_re_append_rows_it_replayed(tmp_path):
    path = tmp_path / "tracker.jsonl"
    t = OpportunityEventTracker(path)
    t.mark_handled("e1", outcome="analyzed", now=T0)
    size_after_one_write = path.stat().st_size

    OpportunityEventTracker(path)
    assert path.stat().st_size == size_after_one_write


def test_every_recorded_row_is_fsynced(tmp_path, monkeypatch):
    """Losing a 'handled' row is not a money risk (agent.analysis.
    run_analysis's own extraction cache already prevents re-paying the
    model for the same document regardless of this tracker) but it is a
    real cost: a lost row can re-surface a duplicate approval request for a
    filing already decided, spending the operator's scarce, capped
    attention (§3.4) a second time on nothing new. Fsync every row, the
    same safe default this codebase applies to every other durable store
    unless a specific reason argues otherwise."""
    import os
    calls = []
    real_fsync = os.fsync
    monkeypatch.setattr(os, "fsync", lambda fd: (calls.append(fd), real_fsync(fd))[1])
    t = store(tmp_path)
    t.mark_handled("e1", outcome="analyzed", now=T0)
    assert len(calls) == 1


def test_store_is_append_only(tmp_path):
    t = store(tmp_path)
    with pytest.raises(OpportunityEventTrackerError):
        t.update()
    with pytest.raises(OpportunityEventTrackerError):
        t.delete()
