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

SIBLING INVALIDATION (§10, Unit 4 item 3) -- INVALIDATE, NOT RECOMPUTE.
With two or more requests pending against the same account, each one's
post-trade figures (reserve, concentration, sector exposure) were computed
assuming the OTHERS were still pending. Approving one changes the ground
truth the others were built on. Two options: silently recompute the
survivors' figures, or invalidate them and require a fresh decision.
Recomputing was rejected: a human may already have read a card's figures
before an approval elsewhere changes them invisibly underneath that
reading -- the same reasoning this codebase already applies to a stale
quote (§3.3: "if the quote moves outside the band... the token is
invalidated and a fresh decision is required") and to an out-of-bounds
modification (§10: "otherwise 'modify' becomes a bypass of the risk
constrainer"). A card whose own numbers can silently change while a human
is looking at it is worse than one that disappears and has to be
re-issued. `decide()` therefore invalidates every OTHER pending request
for the same account the instant one is APPROVED (never on a REJECTED
decision -- rejecting a candidate does not change any other candidate's
post-trade arithmetic), with `invalidated_reason` naming the approved
sibling's `request_id` so the record explains itself to a later reader.

FSYNC: EVERY ROW -- no external source of truth for an approval decision
once made; same reasoning as `agent.cost.CostLedger`/`agent.
analysis_result_store.AnalysisResultStore`.
"""
from __future__ import annotations

import json
import os
import secrets
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path

from .entities import ApprovalRequest


class ApprovalRequestStoreError(Exception):
    pass


class ApprovalRequestStore:
    def __init__(self, path: str | Path):
        self._path = Path(path)
        self._current: dict[str, ApprovalRequest] = {}
        self._account_of: dict[str, str] = {}
        self._created_dates: list[date] = []
        if self._path.exists():
            self._load_into()

    # -- write ----------------------------------------------------------------
    def create(self, *, account_id: str, run_id: str, proposal_snapshot: dict,
              risk_result: dict, price_at_analysis: float, price_band_low: float,
              price_band_high: float, now: datetime, expiration: timedelta,
              request_id: str | None = None, persist: bool = True) -> ApprovalRequest:
        request_id = request_id or f"apr-{secrets.token_hex(12)}"
        if request_id in self._current:
            raise ApprovalRequestStoreError(f"request_id {request_id!r} already exists")
        req = ApprovalRequest(
            request_id=request_id, run_id=run_id, proposal_snapshot=proposal_snapshot,
            risk_result=risk_result, price_at_analysis=price_at_analysis,
            price_band_low=price_band_low, price_band_high=price_band_high,
            shown_at=now, expires_at=now + expiration,
        )
        self._current[request_id] = req
        self._account_of[request_id] = account_id
        self._created_dates.append(now.date())
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
        if persist:
            self._append_event("decided", self._account_of[request_id], updated)

        if decision == "APPROVED":
            account_id = self._account_of[request_id]
            for other_id, other in list(self._current.items()):
                if (other_id != request_id and self._account_of.get(other_id) == account_id
                        and other.decision is None and other.invalidated_reason is None):
                    self.invalidate(other_id, reason=f"sibling_approved:{request_id}",
                                    now=now, persist=persist)
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

    def count_created_on(self, day: date) -> int:
        return sum(1 for d in self._created_dates if d == day)

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
                self._created_dates.append(req.shown_at.date())
            else:
                self._current[req.request_id] = req
                self._account_of.setdefault(req.request_id, account_id)


def _encode(r: ApprovalRequest) -> dict:
    return {
        "request_id": r.request_id, "run_id": r.run_id,
        "proposal_snapshot": r.proposal_snapshot, "risk_result": r.risk_result,
        "price_at_analysis": r.price_at_analysis, "price_band_low": r.price_band_low,
        "price_band_high": r.price_band_high, "shown_at": r.shown_at.isoformat(),
        "expires_at": r.expires_at.isoformat(), "decision": r.decision,
        "decided_by": r.decided_by,
        "decided_at": r.decided_at.isoformat() if r.decided_at else None,
        "decision_elapsed_ms": r.decision_elapsed_ms,
        "invalidated_reason": r.invalidated_reason,
    }


def _decode(row: dict) -> ApprovalRequest:
    return ApprovalRequest(
        request_id=row["request_id"], run_id=row["run_id"],
        proposal_snapshot=row["proposal_snapshot"], risk_result=row["risk_result"],
        price_at_analysis=row["price_at_analysis"], price_band_low=row["price_band_low"],
        price_band_high=row["price_band_high"],
        shown_at=datetime.fromisoformat(row["shown_at"]),
        expires_at=datetime.fromisoformat(row["expires_at"]),
        decision=row["decision"], decided_by=row["decided_by"],
        decided_at=datetime.fromisoformat(row["decided_at"]) if row["decided_at"] else None,
        decision_elapsed_ms=row["decision_elapsed_ms"],
        invalidated_reason=row["invalidated_reason"],
    )
