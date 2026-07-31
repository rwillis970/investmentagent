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
general escape hatch for a malformed or impossible one.

COMMIT 2 (2026-07-31): A NARROW AUTO-ADMIT FOR EXACTLY ONE PATTERN. Every
trade generates a CAT fee (this module's own Commit 3 finding above), so
the operator was hand-admitting one cent per fill, indefinitely -- a
well-understood, correctly-signed, recurring pattern, not a fresh judgment
call each time. `_is_auto_admittable_cat_fee` is the ONLY pattern eligible:
`activity_type == "FEE"`, `activity_sub_type == "CAT"`, `net_amount`
STRICTLY negative (a positive one is unexplained -- a CAT fee is a debit,
never a credit; a positive FEE/CAT row would mean this system's own
understanding of the pattern is wrong, not that it's a bigger version of
the same fee), and `abs(net_amount) <= cat_fee_auto_admit_ceiling` (a
config-supplied magnitude ceiling -- see agent/config.py's own comment for
the chosen default and why it is explicitly NOT a broker-documented
number). Anything not matching exactly still quarantines, with the reason
naming WHICH check failed when the activity was close (wrong sign, over
ceiling) -- see `_disqualifying_reason`.

AUTO-ADMISSION IS STILL SUBJECT TO THE SAME PRE-BASELINE GUARD AS A
MANUAL ADMISSION (Commit 1, agent/cash_event_quarantine.py's own module
docstring -- the $500 JNLC near-double-count). Auto-admitting is
functionally an admission performed without an operator, so it carries
exactly the same double-count risk a manual `--admit-cash-event` does if
the activity predates this account's ledger baseline; skipping this check
for the auto path would silently reopen the hole Commit 1 just closed for
the manual one. A CAT fee that matches the pattern but predates the
baseline therefore still falls through to ordinary quarantine (an
operator can still --reject-cash-event it, exactly as Commit 1 intends) --
it is never auto-admitted.

WHAT THIS DOES NOT CHANGE: `reconcile_settled_cash`'s exact equality (untouched --
an auto-admitted CAT fee is folded into `local_settled_cash` via the exact
same `store.write_cash_adjustment` call a manual admission uses, so
reconciliation sees a correctly-updated local figure, not a bypass of the
check). An unexplained difference the auto-admit pattern does NOT cover
still halts exactly as before.

WHAT AN ATTACKER OR A BROKER BUG COULD DO WITHIN THESE BOUNDS, NAMED
PLAINLY. Anyone who can inject or spoof an Account Activities row this
adapter reads (a compromised or buggy broker API response -- this system
has no way to authenticate the broker's OWN data feed beyond TLS) could
have this system auto-admit any number of FEE/CAT rows, each up to
`cat_fee_auto_admit_ceiling` in magnitude, with no operator ever looking
at any single one -- e.g. many small debits, each individually
unremarkable, draining settled cash over time with nobody explicitly
approving any of them. The ceiling bounds the PER-EVENT damage (never
more than one ceiling's worth silently applied per row) but does NOT bound
the CUMULATIVE damage across many rows, and does not itself detect an
unusual VOLUME of them -- a broker returning a hundred $0.05 FEE/CAT rows
in one poll would auto-admit all hundred with no distinct signal beyond
each one's own audit row. Whether the ceiling makes this acceptable rests
entirely on the broker channel already being trusted infrastructure (this
system already trusts every OTHER read from the same adapter -- account
balance, positions, fills -- with no independent corroboration; a broker
compromise capable of injecting fake activity rows could equally well
report fake fills or a fake account balance, which this system has never
defended against either) -- accepted on that basis, not because the
ceiling makes a compromised broker feed itself safe. NOT ACCEPTED WITHOUT
NAMING: this auto-admit path is a NEW, narrower trust boundary than "poll
and let a human look at it" (Commit 3's own default), and its blast radius
is `cat_fee_auto_admit_ceiling` dollars times however many matching rows
a compromised or malfunctioning broker feed reports, per poll, forever,
with no cumulative cap in this commit."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from .accounts import CrossAccountError
from .audit import AuditLog
from .broker.base import AccountActivity, BrokerAdapter
from .cash_event_quarantine import (ADMITTED, CashEventQuarantineStore,
                                    refuse_admission_reason)
from .ledger import CashAdjustment
from .ledger_store import LedgerStore

# The one pattern eligible for auto-admit (see module docstring's COMMIT 2
# section). Not configurable beyond the magnitude ceiling -- widening this
# to more types/sub-types is a new, separate decision, not a config knob.
_CAT_FEE_TYPE = "FEE"
_CAT_FEE_SUBTYPE = "CAT"


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


def _audit_auto_admission(audit_log: AuditLog, *, account_id: str, activity_id: str,
                          net_amount, ceiling: Decimal, now: datetime) -> None:
    """Distinguishable from `_audit_admission` by BOTH `actor` ("system",
    never "operator") and `action` ("cash_event_auto_admitted", never
    "cash_event_admitted") -- see module docstring's COMMIT 2 section for
    why an auto-admission must never be indistinguishable, in the audit
    trail, from a human's decision."""
    audit_log.append(
        actor="system", action="cash_event_auto_admitted", object_type="cash_event",
        object_id=activity_id,
        after={"account_id": account_id, "net_amount": str(net_amount),
              "cat_fee_auto_admit_ceiling": str(ceiling)},
        timestamp=now,
    )


def _cash_adjustment_for(act: AccountActivity) -> CashAdjustment:
    return CashAdjustment(
        adjustment_id=act.activity_id, account_id=act.account_id,
        amount=act.net_amount, activity_type=act.activity_type,
        description=act.description, effective_date=act.date,
        symbol=act.symbol,
    )


def _disqualifying_reason(act: AccountActivity, *, ceiling: Decimal) -> str | None:
    """Why `act` does NOT qualify for the narrow CAT-fee auto-admit
    pattern -- `None` if it does qualify (on this check alone; the
    pre-baseline guard is checked separately, by the caller). Used to make
    an ordinary quarantine's own `reason` name WHICH check failed, for an
    activity that was close to the pattern but not an exact match --
    "the reason matters" applies here too, not just to Commit 3's own
    quarantine reason."""
    if act.activity_type != _CAT_FEE_TYPE or act.activity_sub_type != _CAT_FEE_SUBTYPE:
        return None   # not a CAT fee at all -- ordinary quarantine reason covers it
    if act.net_amount >= 0:
        return (
            f"matches FEE/CAT but net_amount {act.net_amount} is not negative -- "
            "a CAT fee is always a debit; a non-negative one is unexplained, "
            "not a bigger version of the same pattern"
        )
    if abs(act.net_amount) > ceiling:
        return (
            f"matches FEE/CAT and is negative but |{act.net_amount}| exceeds the "
            f"configured cat_fee_auto_admit_ceiling of {ceiling}"
        )
    return None


def _is_auto_admittable_cat_fee(act: AccountActivity, *, ceiling: Decimal) -> bool:
    return (act.activity_type == _CAT_FEE_TYPE
           and act.activity_sub_type == _CAT_FEE_SUBTYPE
           and act.net_amount < 0
           and abs(act.net_amount) <= ceiling)


def sync_cash_events(adapter: BrokerAdapter, store: LedgerStore, *,
                     now: datetime, quarantine: CashEventQuarantineStore,
                     audit_log: AuditLog,
                     cat_fee_auto_admit_ceiling: Decimal) -> tuple[CashAdjustment, ...]:
    """Read `adapter.non_fill_activities()` and apply/quarantine whatever
    is new. Returns the `CashAdjustment`s newly APPLIED this call, whether
    by an operator's earlier admission or by this call's own auto-admit
    (an activity that was merely quarantined, or already known, is not
    returned) -- mirrors `sync_fills`'s own "returns only what's new"
    contract.

    `quarantine`/`audit_log` are both required, matching `sync_fills`'s own
    "no way to call this and silently skip either" discipline.
    `cat_fee_auto_admit_ceiling` is required too, no default here -- see
    agent/config.py's own comment for the chosen number; this module does
    not invent one of its own."""
    known_ids = store.known_cash_adjustment_ids()
    established_at = store.opening_balance_established_at()

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
            adjustment = _cash_adjustment_for(act)
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

        # Never-before-seen activity: the ONLY point auto-admit is
        # considered (see module docstring's COMMIT 2 section) -- an
        # activity already quarantined on an earlier poll is handled
        # above, unconditionally, regardless of whether it would now
        # qualify. The pre-baseline guard applies here exactly as it does
        # to a manual --admit-cash-event (agent/cash_event_quarantine.py's
        # own module docstring) -- auto-admitting is still an admission.
        pattern_matches = _is_auto_admittable_cat_fee(act, ceiling=cat_fee_auto_admit_ceiling)
        baseline_refusal = refuse_admission_reason(
            activity_id=activity_id, created_at=act.created_at,
            opening_balance_established_at=established_at,
        ) if pattern_matches else None
        if pattern_matches and baseline_refusal is None:
            adjustment = _cash_adjustment_for(act)
            store.write_cash_adjustment(adjustment)
            known_ids = known_ids | {activity_id}
            applied.append(adjustment)
            _audit_auto_admission(
                audit_log, account_id=act.account_id, activity_id=activity_id,
                net_amount=act.net_amount, ceiling=cat_fee_auto_admit_ceiling, now=now,
            )
            continue

        disqualified = baseline_refusal or _disqualifying_reason(
            act, ceiling=cat_fee_auto_admit_ceiling)
        reason = (
            f"unexplained cash movement: {act.activity_type}"
            f"{'/' + act.activity_sub_type if act.activity_sub_type else ''}: "
            f"{act.description}"
        )
        if disqualified is not None:
            reason += f" (not auto-admitted: {disqualified})"
        quarantine.quarantine(act, reason=reason, at=now)
        _audit_quarantine(
            audit_log, account_id=act.account_id, activity_id=activity_id,
            activity_type=act.activity_type, activity_sub_type=act.activity_sub_type,
            net_amount=act.net_amount, reason=reason, now=now,
        )

    return tuple(applied)
