"""Detecting a broker cash movement with no local counterpart, and
quarantining it (Commit 3 of the cash-event quarantine unit, 2026-07-30) --
`sync_cash_events` mirrors `agent.fill_sync.sync_fills`'s own shape exactly:
POLL, not derive. It builds no cadence loop, no scheduler, no process entry
point: it is the function such a loop would call, callable and testable
standalone (`agent.run_loop.run_cycle` wires it in, right after
`sync_fills`/`close_terminal_orders`).

THE DESIGN QUESTION, ANSWERED: WHERE DOES DETECTION BELONG? Two options
were on the table.

  (a) POLL Account Activities for non-FILL types, alongside sync_fills --
      what this module does. Needs a new read (`BrokerAdapter.
      non_fill_activities()`, Commit 3's own broker-layer half), but
      learns the broker's own stated reason (activity_type, sub_type,
      description) for every movement, before anything halts.

  (b) DERIVE from the reconciliation mismatch itself --
      `agent.reconciliation.reconcile_settled_cash` already halts the
      instant local and broker settled cash diverge; a caller could catch
      `ReconciliationMismatch`, compute the delta, and quarantine THAT.
      Reuses an existing check, needs no new broker read.

(a) IS THE RIGHT ANSWER, because the reason matters, not just the number.
`reconcile_settled_cash`'s own mismatch carries exactly one fact: local and
broker settled cash disagree by some amount. It does not, and structurally
cannot, say WHY -- a mismatch of $0.01 is equally consistent with a CAT
fee, a dividend, a corrected/reversed activity, or a local bug in this
system's OWN arithmetic (an off-by-one-cent rounding error, say). Handing
an operator a bare number and asking them to admit-or-reject it is asking
them to TRANSCRIBE, not CONFIRM -- they would have to separately go log
into the broker's dashboard, find the activity that explains the number,
and manually key in what this system could have told them directly. (b)
also structurally CANNOT distinguish "a real, external cash event this
system correctly doesn't know about yet" from "this system's own ledger
math is wrong" -- both look identical from reconciliation's point of view,
a mismatch with no attached reason -- so it would quarantine bugs and real
external activity through the same undifferentiated path, and worse, would
delay quarantining a real activity until the NEXT reconciliation happens
to run (reconciliation only runs once per cycle, after sync_fills/
sync_cash_events, per `agent.run_loop.run_cycle`'s own ordering) --
querying Account Activities directly finds it as soon as the broker has
posted it, not one hop later.

(a)'s real cost, named rather than hidden: a new broker-layer read
(`BrokerAdapter.non_fill_activities()`) that must stay correct on its own,
independent of reconciliation ever running at all -- see
`agent/broker/base.py`'s own docstring for why this is a concrete, not
abstract, method (default `[]`, only `AlpacaPaperAdapter` overrides it with
a real implementation). Accepted: this is exactly the kind of surface
`agent.execution_quarantine.ExecutionQuarantineStore`'s own quarantine
mechanism already established a precedent for needing.

MIRRORS `sync_fills` EXACTLY, DELIBERATELY, NOT A NEW SHAPE. Known-ids
check first (an activity_id already durably recorded via `LedgerStore.
known_cash_adjustment_ids()` is a silent no-op, not re-validated); then
check the quarantine store's own resolution for this id -- ADMITTED
applies it via `store.write_cash_adjustment` (subject to every validation
`Ledger.record_cash_adjustment` already enforces: a wrong account_id, or a
replayed-with-different-contents id, is refused exactly as any other
caller's would be); REJECTED skips it, permanently; otherwise (unresolved)
quarantine it if not already PENDING, and audit the discovery. One
implementation of "poll, diff against what's known, quarantine the rest,"
not two competing ones for fills vs. cash events.

STILL A HARD HALT: a wrong-account activity (`CrossAccountError`) -- same
as `sync_fills`'s own wrong-account execution. Quarantine is for "this
activity is well-formed but nobody has looked at it yet"; it is not a
general escape hatch for a malformed or impossible one."""
from __future__ import annotations

from datetime import datetime

from .accounts import CrossAccountError
from .audit import AuditLog
from .broker.base import BrokerAdapter
from .cash_event_quarantine import ADMITTED, CashEventQuarantineStore
from .ledger import CashAdjustment
from .ledger_store import LedgerStore


def _audit_quarantine(audit_log: AuditLog, *, account_id: str, activity_id: str,
                      activity_type: str, activity_sub_type: str | None,
                      net_amount, reason: str, now: datetime) -> None:
    audit_log.append(
        actor="system", action="cash_event_quarantined", object_type="cash_event",
        object_id=activity_id,
        after={"account_id": account_id, "activity_type": activity_type,
              "activity_sub_type": activity_sub_type, "net_amount": str(net_amount),
              "reason": reason},
        timestamp=now,
    )


def _audit_admission(audit_log: AuditLog, *, account_id: str, activity_id: str,
                     net_amount, now: datetime) -> None:
    audit_log.append(
        actor="operator", action="cash_event_admitted", object_type="cash_event",
        object_id=activity_id,
        after={"account_id": account_id, "net_amount": str(net_amount)},
        timestamp=now,
    )


def sync_cash_events(adapter: BrokerAdapter, store: LedgerStore, *,
                     now: datetime, quarantine: CashEventQuarantineStore,
                     audit_log: AuditLog) -> tuple[CashAdjustment, ...]:
    """Read `adapter.non_fill_activities()` and apply/quarantine whatever
    is new. Returns only the `CashAdjustment`s newly APPLIED this call (an
    activity that was merely quarantined, or already known, is not
    returned) -- mirrors `sync_fills`'s own "returns only what's new"
    contract.

    `quarantine`/`audit_log` are both required, matching `sync_fills`'s own
    "no way to call this and silently skip either" discipline."""
    known_ids = store.known_cash_adjustment_ids()

    applied: list[CashAdjustment] = []
    for act in adapter.non_fill_activities():
        activity_id = act.activity_id
        if activity_id in known_ids:
            continue   # already durably recorded -- silent no-op, not an error

        if act.account_id != store.account_id:
            raise CrossAccountError(store.account_id, act.account_id,
                                    "sync_cash_events")

        resolution = quarantine.resolution_for(activity_id)
        if resolution is not None and resolution.decision == ADMITTED:
            adjustment = CashAdjustment(
                adjustment_id=activity_id, account_id=act.account_id,
                amount=act.net_amount, activity_type=act.activity_type,
                description=act.description, effective_date=act.date,
                symbol=act.symbol,
            )
            store.write_cash_adjustment(adjustment)
            known_ids = known_ids | {activity_id}
            applied.append(adjustment)
            _audit_admission(audit_log, account_id=act.account_id,
                            activity_id=activity_id, net_amount=act.net_amount, now=now)
            continue
        if resolution is not None:
            # REJECTED -- permanently excluded, no CashAdjustment ever
            # written for it.
            continue
        if quarantine.status(activity_id) is not None:
            # PENDING, already quarantined on an earlier poll -- silent
            # no-op, not a re-raise/re-audit.
            continue

        reason = (
            f"unexplained cash movement: {act.activity_type}"
            f"{'/' + act.activity_sub_type if act.activity_sub_type else ''}: "
            f"{act.description}"
        )
        quarantine.quarantine(act, reason=reason, at=now)
        _audit_quarantine(
            audit_log, account_id=act.account_id, activity_id=activity_id,
            activity_type=act.activity_type, activity_sub_type=act.activity_sub_type,
            net_amount=act.net_amount, reason=reason, now=now,
        )

    return tuple(applied)
