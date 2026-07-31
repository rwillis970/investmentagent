"""Quarantine for a broker cash movement that is not a fill and has no
local explanation -- found real, not hypothetical, running the loop
against the real paper account (2026-07-30): a Consolidated Audit Trail
(CAT) regulatory fee posted overnight against a fractional SPY buy
(`scripts/fixtures/activities_since.json`):

    activity_type: FEE, activity_sub_type: CAT, net_amount: "-0.01",
    date: 2026-07-28
    description: "CAT fee for proceed of 1 trades on 2026-07-28 by PA3XZX944LRR"

`agent.reconciliation.reconcile_settled_cash`'s deliberate exact-equality
check correctly halts the instant this posts, because the local ledger has
no way to account for a $0.01 movement it never recorded. CORRECT, BUT NOT
AN EDGE CASE: a CAT fee is charged PER TRADE (confirmed by Alpaca's own fee
description above, "for proceed of 1 trades"), so this halt recurs on
every single fill this system ever makes, not once. Treating it as fatal
(an uncaught `ReconciliationMismatch` propagating out of `run_startup`)
would halt the scheduled loop forever on the very first trade and every
one after -- the same shape of problem `agent.execution_quarantine.
ExecutionQuarantineStore` already solved for an execution with no
resolvable intent, applied here to a cash movement with no local
counterpart at all.

WHY QUARANTINE, NOT AUTO-INGEST. A broker-reported cash movement could, in
principle, be folded into the local ledger automatically the moment it's
observed -- no operator step at all. Rejected for the same reason
`ExecutionQuarantineStore` rejected an automatic default for a BUY: Alpaca's
own activity feed carries MANY different `activity_type`s (FEE, DIV, INT,
JNLC, CSD, REORG, and dozens more per the probe's own capture -- see
`scripts/alpaca_probe.py`'s `DEFAULT_ACTIVITIES_SINCE` capture), not all of
which are cash-only, not all of which are correctly signed the way a fee
is, and some of which (a corporate action, a corrected/reversed activity)
would be actively wrong to fold into settled cash without a human looking
at the broker's own stated reason first. Auto-ingesting the common case
(a small, well-understood recurring fee) while asking a human about
everything else would be a SECOND, competing answer to "how does a cash
movement reach the ledger" -- exactly the kind of duplication this
codebase's control architecture avoids elsewhere (one path from store to
orders; one disposal-order computation; one quarantine mechanism for an
unresolvable execution). One mechanism, covering every non-FILL activity
type uniformly, with a human in the loop before any of them affects
settled cash, is both simpler and more conservative -- consistent with
Appendix E's fail-safe-to-NO-TRADE bias. The real, narrow cost: a genuine,
correctly-signed, recurring fee still needs an operator's confirm on every
new activity_id, not just the first time this activity_type is seen --
accepted here as the trade for never silently applying a movement nobody
has looked at (a future unit MAY choose to auto-admit prior-vintage
activity_types once thoroughly reviewed; not attempted here, and this
module does not need to be re-architected for that -- see
`agent.cash_events.sync_cash_events`, which decides what's new, not this
store).

ADMISSION IS A CONFIRM, NOT A FILL-IN-THE-BLANK -- the one structural
difference from `ExecutionQuarantineStore.admit`. An unresolvable
execution is missing a fact only a human (or the strategy layer) can
supply -- `lot_id` for a SELL, `holding_policy_version` for a BUY -- so
`admit()` there REQUIRES the operator to name it. A cash event has no such
gap: the broker's own activity record already carries every field this
system needs (`activity_type`, `activity_sub_type`, `net_amount`, `date`,
`symbol`, `description`) -- the only open question is whether an operator
has reviewed the broker's own stated reason and accepts it. `admit()` here
therefore takes no domain field at all beyond who decided and when; the
system pre-fills amount, type and the broker's stated reason (via
`pending()`/`load()`), and the operator's decision is confirm-or-reject,
never transcribe-a-number (`scripts.run_agent --admit-cash-event` mirrors
this shape).

TWO DURABILITY POSTURES IN ONE FILE, SAME REASONING AS
`agent.execution_quarantine.ExecutionQuarantineStore` (see that module's
own docstring for the full argument, not repeated here). A quarantine ROW
is reconstructible from the broker at any time -- `adapter.
non_fill_activities()` will report the same activity again on the next
poll -- so quarantine rows are `flush()`-only, no fsync: a completeness
gap, not a safety one. A RESOLUTION row (an operator's admit/reject
decision) has no external source of truth, so resolution rows fsync on
every write: losing a buffered decision on an unclean shutdown must not
silently un-decide it.

APPEND-ONLY, REPLAY-VALIDATED ON LOAD. Same discipline as
`ExecutionQuarantineStore`/`LedgerStore`: every write reaches disk only
after `quarantine`/`admit`/`reject`'s own validation accepts it, and
`_load_into` replays a file's rows through those SAME three methods, so a
hand-edited or corrupted row is refused at load time too.

EXACTLY ONE RESOLUTION EVER WINS, same as `ExecutionQuarantineStore`: a
cash event is resolved exactly once; `admit`/`reject` refuse to record a
SECOND, DIFFERENT decision for the same `activity_id` (an identical
replay is a safe no-op).

A PRE-BASELINE CASH EVENT MUST NEVER BE ADMITTED (found real, 2026-07-31,
running the loop against the real paper account to a clean reconcile).
`sync_cash_events` quarantined not just the CAT fee this module's own
docstring above already describes, but ALSO the $500 JNLC deposit that
this very account's opening balance was itself seeded from. That $500 is
already inside `LedgerStore`'s opening_settled_cash (the broker read used
to seed it already reflected the deposit); admitting it as a SECOND,
separate `CashAdjustment` would have put local settled cash at $980,
double-counted. It was rejected only because the operator happened to
recognise what the activity_id was -- exactly the "transcribe, not
confirm" failure mode this store's own ADMISSION IS A CONFIRM section
above already argues against. A quarantined cash event dated at or before
the point its account's ledger baseline was established is not a judgment
call an operator's domain knowledge could ever correctly override in the
other direction -- it is ALWAYS already reflected in that baseline, full
stop -- so `refuse_admission_reason` (module-level function, below) makes
this refusal unconditional rather than a warning an operator could admit
past.

WHY THIS CHECK IS NOT INSIDE `admit()` ITSELF. `admit()`/`_load_into`'s
replay of a durably-recorded ADMITTED row must stay self-contained and
side-effect-free of any OTHER store's state -- `CashEventQuarantineStore`
has never been, and does not become here, coupled to `agent.ledger_store.
LedgerStore` (the "own file, own class" isolation `ModeStore`/
`ExecutionQuarantineStore`/this store all already share). Reading a
SEPARATE store's file just to replay an already-decided resolution would
be a new, load-bearing cross-store dependency for no benefit: a decision
already durably admitted was already checked once, at the actual moment
of admission (`scripts/run_agent.py`'s `--admit-cash-event`, which DOES
have both stores in hand). `refuse_admission_reason` is therefore a bare
function taking only the two datetimes that matter (`created_at`,
`opening_balance_established_at`) -- callable by that CLI layer before it
ever calls `.admit()`, and, since it takes no store at all, equally
reusable by `agent.cash_events.sync_cash_events`'s own narrower auto-admit
path (Commit 2, same file) without either needing to know about the
other.

WHY `created_at`, NOT `date` (see agent/broker/base.py's own docstring's
`created_at` section for the full argument). The broker's settled-cash
figure at the instant the opening balance was read already reflects every
activity the broker's books had POSTED by that instant -- governed by
`created_at`, not by `date` (the CAT fee's own economically-attributed
day, which can be, and in the one real case observed already IS, earlier
than when the broker's batch job actually posted it). Comparing by `date`
would have gotten that same CAT fee wrong in the unsafe direction: its
`date` (2026-07-28) could equal a same-day opening-balance read taken
before the fee posted (`created_at` 2026-07-29T00:07), wrongly refusing an
admission that was actually still owed -- permanently starving local
settled cash of an effect the broker's own figure does eventually apply,
which would halt `reconcile_settled_cash`'s exact equality forever, not
just once."""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from .accounts import CrossAccountError
from .broker.base import AccountActivity
from .money import to_decimal

PENDING, ADMITTED, REJECTED = "PENDING", "ADMITTED", "REJECTED"
_DECISIONS = (ADMITTED, REJECTED)


class CashEventQuarantineError(Exception):
    pass


@dataclass(frozen=True)
class QuarantinedCashEvent:
    """The broker's non-FILL account activity as reported, plus why
    `agent.cash_events.sync_cash_events` could not safely fold it into
    settled cash on its own. Never mutated -- see module docstring."""
    activity_id: str
    account_id: str
    activity_type: str
    activity_sub_type: str | None
    net_amount: Decimal
    date: date
    created_at: datetime
    symbol: str | None
    description: str
    reason: str
    quarantined_at: datetime


@dataclass(frozen=True)
class CashEventResolution:
    """An operator's one, permanent decision about a quarantined cash
    event. `decision` is `"ADMITTED"` or `"REJECTED"`. No domain field is
    ever required here -- see module docstring's ADMISSION IS A CONFIRM
    section for why this differs from `agent.execution_quarantine.
    ExecutionResolution`."""
    activity_id: str
    account_id: str
    decision: str
    decided_by: str
    decided_at: datetime
    notes: str | None = None


class CashEventQuarantineStore:
    """Append-only, per-account (like `ExecutionQuarantineStore`/
    `LedgerStore`/`ModeStore`). Own file, own class -- see module docstring
    for why this is not folded into `ExecutionQuarantineStore` (a
    cash-only movement is not an execution: it has no `client_order_id`,
    no `side`, no `qty`/`price` pair, and needs no lot_id/
    holding_policy_version to resolve)."""

    def __init__(self, path: str | Path, *, account_id: str):
        self._path = Path(path)
        self.account_id = account_id
        self._quarantined: dict[str, QuarantinedCashEvent] = {}
        self._resolutions: dict[str, CashEventResolution] = {}
        if self._path.exists():
            self._load_into()

    # -- write ---------------------------------------------------------------
    def quarantine(self, activity: AccountActivity, *, reason: str,
                  at: datetime) -> QuarantinedCashEvent:
        """Idempotent: re-quarantining the SAME activity_id with identical
        details is a safe no-op (a re-poll seeing the same unresolved
        activity again) -- it does NOT overwrite `reason`/`quarantined_at`
        with the later call's values, matching `ExecutionQuarantineStore.
        quarantine`'s own "identical replay is a no-op" rule."""
        if activity.account_id != self.account_id:
            raise CrossAccountError(self.account_id, activity.account_id,
                                    "CashEventQuarantineStore.quarantine")
        existing = self._quarantined.get(activity.activity_id)
        record = QuarantinedCashEvent(
            activity_id=activity.activity_id, account_id=activity.account_id,
            activity_type=activity.activity_type,
            activity_sub_type=activity.activity_sub_type,
            net_amount=to_decimal(activity.net_amount), date=activity.date,
            created_at=activity.created_at,
            symbol=activity.symbol, description=activity.description,
            reason=reason, quarantined_at=at,
        )
        if existing is not None:
            return existing   # already known -- silent no-op, not an error
        self._append_row(dict(kind="quarantined", **_encode_quarantined(record)),
                         fsync=False)
        self._quarantined[record.activity_id] = record
        return record

    def admit(self, activity_id: str, *, decided_by: str, decided_at: datetime,
             notes: str | None = None) -> CashEventResolution:
        """Confirms a fully system-proposed cash adjustment -- no domain
        field is required or accepted here (see module docstring). Whether
        the admitted event actually produces a valid ledger adjustment is
        NOT checked here -- that is `Ledger.record_cash_adjustment`'s job,
        the next time `agent.cash_events.sync_cash_events` runs; this store
        only records the DECISION."""
        record = self._require_quarantined(activity_id)
        resolution = CashEventResolution(
            activity_id=activity_id, account_id=record.account_id,
            decision=ADMITTED, decided_by=decided_by, decided_at=decided_at,
            notes=notes,
        )
        return self._record_resolution(resolution)

    def reject(self, activity_id: str, *, decided_by: str, decided_at: datetime,
              notes: str | None = None) -> CashEventResolution:
        """Permanently excludes this activity from the ledger -- `agent.
        cash_events.sync_cash_events` never applies it, no matter how many
        more times it is reported by a future poll."""
        record = self._require_quarantined(activity_id)
        resolution = CashEventResolution(
            activity_id=activity_id, account_id=record.account_id,
            decision=REJECTED, decided_by=decided_by, decided_at=decided_at,
            notes=notes,
        )
        return self._record_resolution(resolution)

    def _require_quarantined(self, activity_id: str) -> QuarantinedCashEvent:
        record = self._quarantined.get(activity_id)
        if record is None:
            raise CashEventQuarantineError(
                f"cash event {activity_id!r} was never quarantined on this store "
                "-- nothing to resolve"
            )
        return record

    def _record_resolution(self, resolution: CashEventResolution) -> CashEventResolution:
        existing = self._resolutions.get(resolution.activity_id)
        if existing is not None:
            if existing == resolution:
                return existing   # identical replay -- safe no-op
            raise CashEventQuarantineError(
                f"cash event {resolution.activity_id!r} was already resolved "
                f"({existing.decision}); a resolution is permanent and does not "
                "get a second, different decision"
            )
        self._append_row(dict(kind="resolution", **_encode_resolution(resolution)),
                         fsync=True)
        self._resolutions[resolution.activity_id] = resolution
        return resolution

    # -- read ------------------------------------------------------------------
    def status(self, activity_id: str) -> str | None:
        """`None` if never quarantined at all; else PENDING/ADMITTED/REJECTED."""
        if activity_id not in self._quarantined:
            return None
        resolution = self._resolutions.get(activity_id)
        return resolution.decision if resolution is not None else PENDING

    def resolution_for(self, activity_id: str) -> CashEventResolution | None:
        return self._resolutions.get(activity_id)

    def pending(self) -> tuple[QuarantinedCashEvent, ...]:
        return tuple(q for aid, q in self._quarantined.items()
                    if aid not in self._resolutions)

    def load(self) -> tuple[tuple[QuarantinedCashEvent, ...], tuple[CashEventResolution, ...]]:
        return tuple(self._quarantined.values()), tuple(self._resolutions.values())

    def update(self, *a, **k):
        raise CashEventQuarantineError("append-only; write a new row")

    def delete(self, *a, **k):
        raise CashEventQuarantineError("append-only; rows are never deleted")

    # -- persistence -------------------------------------------------------
    def _append_row(self, row: dict, *, fsync: bool) -> None:
        with self._path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")
            fh.flush()
            if fsync:
                os.fsync(fh.fileno())

    def _load_into(self) -> None:
        """Read the whole file before replaying anything (same reasoning
        as `ExecutionQuarantineStore._load_into`/`LedgerStore._load_into`:
        the reader must never observe a row written during its own
        replay). Rows are replayed THROUGH `quarantine`/`admit`/`reject` --
        the same validated path a fresh write goes through."""
        with self._path.open(encoding="utf-8") as fh:
            lines = [ln for ln in fh.read().splitlines() if ln.strip()]
        for line in lines:
            row = json.loads(line)
            kind = row.get("kind")
            if kind == "quarantined":
                self.quarantine(_decode_activity(row), reason=row["reason"],
                                at=datetime.fromisoformat(row["quarantined_at"]))
            elif kind == "resolution":
                if row["decision"] == ADMITTED:
                    self.admit(row["activity_id"], decided_by=row["decided_by"],
                              decided_at=datetime.fromisoformat(row["decided_at"]),
                              notes=row.get("notes"))
                elif row["decision"] == REJECTED:
                    self.reject(row["activity_id"], decided_by=row["decided_by"],
                               decided_at=datetime.fromisoformat(row["decided_at"]),
                               notes=row.get("notes"))
                else:
                    raise CashEventQuarantineError(
                        f"unrecognised resolution decision {row['decision']!r}"
                    )
            else:
                raise CashEventQuarantineError(
                    f"unrecognised cash event quarantine row kind {kind!r} -- "
                    "refusing to silently skip a row this version does not "
                    "understand"
                )


def _encode_quarantined(record: QuarantinedCashEvent) -> dict:
    d = asdict(record)
    # Decimal/date are not JSON-native -- str()/isoformat() round-trip
    # exactly, the same discipline agent/ledger_store.py's _encode_fill and
    # agent/execution_quarantine.py's _encode_quarantined already follow.
    d["net_amount"] = str(record.net_amount)
    d["date"] = record.date.isoformat()
    d["created_at"] = record.created_at.isoformat()
    d["quarantined_at"] = record.quarantined_at.isoformat()
    return d


def _decode_activity(row: dict) -> AccountActivity:
    return AccountActivity(
        activity_id=row["activity_id"], account_id=row["account_id"],
        activity_type=row["activity_type"],
        activity_sub_type=row.get("activity_sub_type"),
        net_amount=to_decimal(row["net_amount"]), date=date.fromisoformat(row["date"]),
        created_at=datetime.fromisoformat(row["created_at"]),
        symbol=row.get("symbol"), description=row["description"],
    )


def refuse_admission_reason(*, activity_id: str, created_at: datetime,
                            opening_balance_established_at: datetime | None) -> str | None:
    """Returns a human-readable refusal reason if a cash event dated
    `created_at` is already reflected in the ledger's own opening balance
    (established at `opening_balance_established_at`) -- i.e. admitting it
    would double-count it -- or `None` if admission may proceed. See this
    module's own docstring's "A PRE-BASELINE CASH EVENT MUST NEVER BE
    ADMITTED" and "WHY `created_at`, NOT `date`" sections for the full
    reasoning and the real incident this was found from.

    `opening_balance_established_at=None` means this account's ledger has
    never been seeded yet (`agent.ledger_store.LedgerStore.
    opening_balance_established_at`/`read_opening_balance_established_at`
    both use this same "never seeded" meaning) -- there is no baseline yet
    for anything to predate, so this always returns `None` (admission may
    proceed; nothing here overrides `Ledger.record_cash_adjustment`'s own,
    separate validation).

    Deliberately a bare function, not a `CashEventQuarantineStore` method --
    see module docstring's "WHY THIS CHECK IS NOT INSIDE admit() ITSELF"
    for why, and for why this is reused by BOTH `scripts.run_agent`'s
    `--admit-cash-event` (Commit 1) and `agent.cash_events.sync_cash_events`'s
    own auto-admit path (Commit 2) rather than checked in either store."""
    if opening_balance_established_at is None:
        return None
    if created_at <= opening_balance_established_at:
        return (
            f"cash event {activity_id!r} (created_at={created_at.isoformat()}) is "
            f"at or before the opening balance's own establishment instant "
            f"({opening_balance_established_at.isoformat()}) -- its cash effect is "
            "already reflected in the seeded baseline; admitting it would "
            "double-count it. Reject it instead."
        )
    return None


def _encode_resolution(resolution: CashEventResolution) -> dict:
    d = asdict(resolution)
    d["decided_at"] = resolution.decided_at.isoformat()
    return d
