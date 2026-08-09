"""Durable persistence for `agent.entities.ApprovalRequest` (unattended
wiring unit, 2026-08-01) -- the Day-1 entity/table (`migrations/
001_init.sql`'s `agent.approval_request`) that nothing has ever constructed
until this unit's Unit 4.

CREATE-THEN-RESOLVE, LIKE THE QUARANTINE STORES -- NOT A PLAIN APPEND-ONLY
HISTORY LOG. Unlike `agent.analysis_result_store.AnalysisResultStore` (every
call is a fresh, independent attempt), an `ApprovalRequest` has real state
that changes over its own lifetime: created -> {approved, rejected,
invalidated}, exactly once. This store follows `agent.execution_quarantine.
ExecutionQuarantineStore`'s own shape: append an EVENT per state change
("created"/"decided"/"invalidated"), replay folds them (in order) into the
CURRENT `ApprovalRequest` per `request_id`, and a request already decided or
invalidated refuses a second event for the same reason a resolved quarantine
entry refuses a second resolution.

`shown_at` IS SERVER-RECORDED, NEVER CALLER-SUPPLIED -- set once, at
`create()`, to the `now` this store itself receives (the instant the
request actually came into existence, which is the instant a card can
first be surfaced). Every later friction check in this codebase (§10:
minimum display time, decision_elapsed_ms) is anchored to THIS value, not
to a `shown_at` a UI reports -- see `agent/approval.py`'s own
`verify_minimum_display_time` for the consumption-time half of this.

`decision_elapsed_ms` IS LOGGED ON EVERY DECISION, NOT ONLY AN APPROVAL --
`decide()` computes it from `now - shown_at` for BOTH "APPROVED" and
"REJECTED", closing the gap where `agent.approval.ApprovalService.approve`
only ever recorded it for the approved case (§3.4's own median-decision-time
metric needs rejected decisions in the sample too, not just approved ones).

SIBLING INVALIDATION -- REMOVED (earmarking unit, 2026-08-02). Two or more
requests pending against the same account used to have their post-trade
figures (reserve, concentration, sector exposure) computed assuming the
OTHERS were not pending at all -- approving one changed the ground truth
the others were built on, and `decide()` used to paper over that by
INVALIDATING every other pending request for the same account the instant
one was APPROVED, forcing a fresh decision on each. That was a symptom
fix, not the real one: the underlying defect was that a pending BUY
consumed no accounted-for cash at all until it was actually submitted, so
two pending requests could each be priced as though the other did not
exist. Earmarking (`outstanding_earmarks` below, and `agent.risk.
PortfolioState.pending_buy_notional`, wired for the first time this unit)
removes the need for invalidation directly: a pending request's post-trade
figures now already net out every OTHER pending request's earmark (see
`agent.approval_trigger._post_trade_state`), so approving one no longer
changes any sibling's arithmetic, and there is nothing left for
invalidation to correct. `invalidate()` itself is UNCHANGED and stays --
it is still the right mechanism for a stale quote (price drifted outside
the band) or an expired card, neither of which this unit touches.

FSYNC: EVERY ROW -- no external source of truth for an approval decision
once made; same reasoning as `agent.cost.CostLedger`/`agent.
analysis_result_store.AnalysisResultStore`.

DURABLE TOKEN-MINT RECORD (Unit 2, 2026-08-09). `record_token_minted`
persists that a token was minted for a request -- see `agent.entities.
ApprovalRequest.token_snapshot`'s own docstring and `agent.approval_bridge.
mint_approval_token`'s for why: `agent.approval.ApprovalService._tokens` is
in-memory only, so nothing previously stopped a second `approve()` call
against a fresh `ApprovalService` instance (a real restart) from minting a
second, independently-spendable token for an already-approved request.
This store is the durable side of that fix -- a new "token_minted" event,
replayed the same generic way `_load_into` already replays "decided"/
"invalidated" (no special-casing needed there).

`count_decided_on` (renamed from `count_created_on`, earmarking unit,
2026-08-02) COUNTS DECIDED REQUESTS ONLY -- APPROVED or REJECTED --
NOT EVERY REQUEST CREATED. `max_approval_requests_per_day` exists to
protect a scarce resource: operator attention (§3.4). A card that expired
unread, or was invalidated, or was suppressed before a request ever
existed, spent none of that attention -- only a request an operator
actually DECIDED did. The old `count_created_on` counted every row
appended by `create()`, regardless of whether anything ever came of it;
this store now tracks `_decided_dates`, appended to by `decide()` (for
both APPROVED and REJECTED -- `decision_elapsed_ms` is logged for both
already, for the same "the cap protects attention spent either way"
reason), keyed on `market_calendar.session_for_instant(now)` exactly as
before. `count_decided_on`'s signature is otherwise the same shape as the
old method (`day: date`); every caller (`agent.approval_trigger.
request_approval_for_analysis`'s rate-limit recheck, `agent.
pipeline_stage.run_pipeline_stage`'s `approvals_today` computation feeding
`agent.materiality.screen`) is updated the same commit. The
`pipeline_stage` caller had an independent, pre-existing bug fixed
incidentally here too: it was calling the old method with a raw
`now.date()` (a UTC calendar date), not `market_calendar.
session_for_instant(now)` -- the exact defect the cleanup unit (review
round 3) fixed at this store's OTHER caller but not this one, since only
`approval_trigger.py`'s call site was in that unit's scope. See this
unit's own report.
"""
from __future__ import annotations

import json
import os
import secrets
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from . import market_calendar
from .entities import ApprovalRequest

if TYPE_CHECKING:
    # Type-only: `agent.approval.ApprovalService` imports THIS module (it
    # constructs `ApprovalRequestStore` type hints), so a real, module-level
    # import here would be circular. `from __future__ import annotations`
    # (above) already makes every annotation in this file lazy/string, so
    # this guarded import is safe and only ever runs for a type checker.
    from .approval import ApprovalService


class ApprovalRequestStoreError(Exception):
    pass


class ApprovalRequestStore:
    def __init__(self, path: str | Path):
        self._path = Path(path)
        self._current: dict[str, ApprovalRequest] = {}
        self._account_of: dict[str, str] = {}
        self._decided_dates: list[date] = []
        if self._path.exists():
            self._load_into()

    # -- write ----------------------------------------------------------------
    def create(self, *, account_id: str, run_id: str, proposal_snapshot: dict,
              risk_result: dict, price_at_analysis: float, price_band_low: float,
              price_band_high: float, now: datetime, expiration: timedelta,
              earmark: float = 0.0, request_id: str | None = None,
              persist: bool = True) -> ApprovalRequest:
        request_id = request_id or f"apr-{secrets.token_hex(12)}"
        if request_id in self._current:
            raise ApprovalRequestStoreError(f"request_id {request_id!r} already exists")
        req = ApprovalRequest(
            request_id=request_id, run_id=run_id, proposal_snapshot=proposal_snapshot,
            risk_result=risk_result, price_at_analysis=price_at_analysis,
            price_band_low=price_band_low, price_band_high=price_band_high,
            earmark=earmark, shown_at=now, expires_at=now + expiration,
        )
        self._current[request_id] = req
        self._account_of[request_id] = account_id
        if persist:
            self._append_event("created", account_id, req)
        return req

    def decide(self, request_id: str, *, decision: str, now: datetime,
              decided_by: str, persist: bool = True) -> ApprovalRequest:
        if decision not in ("APPROVED", "REJECTED"):
            raise ApprovalRequestStoreError(f"unknown decision {decision!r}")
        current = self._require(request_id)
        if current.decision is not None:
            raise ApprovalRequestStoreError(
                f"request {request_id} was already decided ({current.decision})"
            )
        if current.invalidated_reason is not None:
            raise ApprovalRequestStoreError(
                f"request {request_id} was invalidated ({current.invalidated_reason}); "
                "a fresh request is required"
            )
        elapsed_ms = int((now - current.shown_at).total_seconds() * 1000)
        updated = replace(current, decision=decision, decided_by=decided_by,
                          decided_at=now, decision_elapsed_ms=elapsed_ms)
        self._current[request_id] = updated
        # Counts against the daily cap regardless of APPROVED/REJECTED -- see
        # module docstring's `count_decided_on` section: the cap protects
        # operator attention, and a decision spends it either way.
        self._decided_dates.append(market_calendar.session_for_instant(now))
        if persist:
            self._append_event("decided", self._account_of[request_id], updated)
        return updated

    def record_token_minted(self, request_id: str, *, token_snapshot: dict,
                            now: datetime, persist: bool = True) -> ApprovalRequest:
        """Durably records that a token was minted for `request_id` (Unit 2,
        2026-08-09) -- see `agent.entities.ApprovalRequest.token_snapshot`'s
        own docstring for why this exists. Called ONLY by `agent.
        approval_bridge.mint_approval_token`, immediately after a successful
        `ApprovalService.approve()`, and ONLY when no snapshot is already
        recorded (that caller checks first; this method still refuses a
        second call defensively, the same posture `decide()` takes against a
        second decision). `now` is accepted for signature symmetry with
        `decide()`/`invalidate()` but is not currently stored -- the token's
        own `decided_at` field, inside `token_snapshot`, already carries the
        mint instant."""
        current = self._require(request_id)
        if current.decision != "APPROVED":
            raise ApprovalRequestStoreError(
                f"request {request_id} is not approved (decision="
                f"{current.decision!r}); cannot record a token mint"
            )
        if current.token_snapshot is not None:
            raise ApprovalRequestStoreError(
                f"request {request_id} already has a recorded token mint; "
                "refusing to overwrite it"
            )
        updated = replace(current, token_snapshot=token_snapshot)
        self._current[request_id] = updated
        if persist:
            self._append_event("token_minted", self._account_of[request_id], updated)
        return updated

    def invalidate(self, request_id: str, *, reason: str, now: datetime,
                   persist: bool = True) -> ApprovalRequest:
        current = self._require(request_id)
        if current.decision is not None:
            raise ApprovalRequestStoreError(
                f"request {request_id} was already decided ({current.decision}); "
                "cannot invalidate a decided request"
            )
        if current.invalidated_reason is not None:
            return current   # idempotent: already invalidated, nothing new to record
        updated = replace(current, invalidated_reason=reason)
        self._current[request_id] = updated
        if persist:
            self._append_event("invalidated", self._account_of[request_id], updated)
        return updated

    def update(self, *a, **k):
        raise ApprovalRequestStoreError("state changes go through create/decide/invalidate")

    def delete(self, *a, **k):
        raise ApprovalRequestStoreError("append-only; rows are never deleted")

    # -- read ---------------------------------------------------------------
    def get(self, request_id: str) -> ApprovalRequest | None:
        return self._current.get(request_id)

    def all(self) -> tuple[ApprovalRequest, ...]:
        return tuple(self._current.values())

    def pending(self, *, account_id: str | None = None,
               now: datetime | None = None) -> tuple[ApprovalRequest, ...]:
        out = []
        for rid, req in self._current.items():
            if req.decision is not None or req.invalidated_reason is not None:
                continue
            if now is not None and now >= req.expires_at:
                continue
            if account_id is not None and self._account_of.get(rid) != account_id:
                continue
            out.append(req)
        return tuple(out)

    def count_decided_on(self, day: date) -> int:
        return sum(1 for d in self._decided_dates if d == day)

    def outstanding_earmarks(self, account_id: str, now: datetime, *,
                            service: "ApprovalService | None" = None) -> float:
        """The total settled cash reserved by every pending BUY request for
        `account_id` -- pending, not expired, not decided, not invalidated
        (the same filter `pending()` already applies; a SELL/CLOSE request's
        `earmark` is always `0.0`, so it contributes nothing here without a
        separate side check). See `agent.risk.PortfolioState.
        pending_buy_notional`, wired from this method's return value in
        `agent.approval_trigger.request_approval_for_analysis`.

        TOKEN HANDOFF (bridge unit, 2026-08-02, Prompt 3 -- this is the unit
        this method's own previous docstring named as deferred). `pending()`
        alone excludes any DECIDED request, approved or rejected, the
        instant it is decided -- correct for a REJECTED request (nothing was
        ever going to consume that cash) but wrong for an APPROVED one: an
        approved-but-unspent token must not free cash a live order is about
        to consume. Passing `service` (an `agent.approval.ApprovalService`)
        folds in every APPROVED request in THIS store whose `agent.
        approval_bridge`-minted token (`service.token_for_request(rid)`)
        still holds its earmark -- not yet consumed, not past its own
        `expires_at`, not swept. `service=None` (the default) preserves this
        method's EXACT prior behaviour -- an approved request's earmark
        releases the instant `pending()` excludes it -- for a caller with no
        service to pass; see this unit's own report for which real caller
        (`agent.approval_trigger.request_approval_for_analysis`) that still
        is today, and why."""
        total = sum(req.earmark for req in self.pending(account_id=account_id, now=now))
        if service is not None:
            for rid, req in self._current.items():
                if self._account_of.get(rid) != account_id:
                    continue
                if req.decision != "APPROVED":
                    continue
                tok = service.token_for_request(rid)
                if tok is None:
                    continue
                if tok.consumed_at is not None or tok.swept_at is not None:
                    continue
                if now >= tok.expires_at:
                    continue
                total += req.earmark
        return total

    def _require(self, request_id: str) -> ApprovalRequest:
        current = self._current.get(request_id)
        if current is None:
            raise ApprovalRequestStoreError(f"unknown request_id {request_id!r}")
        return current

    # -- persistence -------------------------------------------------------
    def _append_event(self, event: str, account_id: str, req: ApprovalRequest) -> None:
        with self._path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "event": event, "account_id": account_id, "request": _encode(req),
            }) + "\n")
            fh.flush()
            os.fsync(fh.fileno())

    def _load_into(self) -> None:
        with self._path.open(encoding="utf-8") as fh:
            lines = [ln for ln in fh.read().splitlines() if ln.strip()]
        for line in lines:
            row = json.loads(line)
            req = _decode(row["request"])
            account_id = row["account_id"]
            event = row["event"]
            if event == "created":
                self._current[req.request_id] = req
                self._account_of[req.request_id] = account_id
            else:
                self._current[req.request_id] = req
                self._account_of.setdefault(req.request_id, account_id)
                if event == "decided" and req.decided_at is not None:
                    self._decided_dates.append(
                        market_calendar.session_for_instant(req.decided_at))


def _encode(r: ApprovalRequest) -> dict:
    return {
        "request_id": r.request_id, "run_id": r.run_id,
        "proposal_snapshot": r.proposal_snapshot, "risk_result": r.risk_result,
        "price_at_analysis": r.price_at_analysis, "price_band_low": r.price_band_low,
        "price_band_high": r.price_band_high, "earmark": r.earmark,
        "shown_at": r.shown_at.isoformat(),
        "expires_at": r.expires_at.isoformat(), "decision": r.decision,
        "decided_by": r.decided_by,
        "decided_at": r.decided_at.isoformat() if r.decided_at else None,
        "decision_elapsed_ms": r.decision_elapsed_ms,
        "invalidated_reason": r.invalidated_reason,
        "token_snapshot": r.token_snapshot,
    }


def _decode(row: dict) -> ApprovalRequest:
    return ApprovalRequest(
        request_id=row["request_id"], run_id=row["run_id"],
        proposal_snapshot=row["proposal_snapshot"], risk_result=row["risk_result"],
        price_at_analysis=row["price_at_analysis"], price_band_low=row["price_band_low"],
        price_band_high=row["price_band_high"], earmark=row.get("earmark", 0.0),
        shown_at=datetime.fromisoformat(row["shown_at"]),
        expires_at=datetime.fromisoformat(row["expires_at"]),
        decision=row["decision"], decided_by=row["decided_by"],
        decided_at=datetime.fromisoformat(row["decided_at"]) if row["decided_at"] else None,
        decision_elapsed_ms=row["decision_elapsed_ms"],
        invalidated_reason=row["invalidated_reason"],
        # .get, not [] -- a row written before Unit 2 (2026-08-09) has no
        # "token_snapshot" key at all; absence means "no token minted yet
        # under the old, in-memory-only regime", never a fabricated mint.
        token_snapshot=row.get("token_snapshot"),
    )
