"""Operator dashboard: approve/reject decision logic (§10; operator
decision surface unit, 2026-08-03). No HTTP concept lives here -- this
module raises `DecisionError`/`DecisionConflict`, and `agent.
dashboard_server` maps those to 4xx responses; that separation is what
makes this module directly unit-testable without an HTTP client.

APPROVE GOES THROUGH `agent.approval_bridge.mint_approval_token` AND
NOTHING ELSE -- the only call in this module (or anywhere else in this
codebase, per that module's own "only production caller" note) that ever
reaches `ApprovalService.approve`. This module's own job is everything
AROUND that call: deciding the request first, validating the modify-
within-bounds fields, being idempotent, and refusing a too-early approve
before the request's own decision/cap-slot is ever spent.

FRICTION IS CHECKED HERE TOO, BEFORE `store.decide()` -- NOT INSTEAD OF THE
AUTHORITATIVE CHECKS ELSEWHERE. `ApprovalService.approve` (called via the
bridge) already refuses to mint a token before `min_display` has elapsed,
and `agent.approval.verify_minimum_display_time` re-checks the identical
property again at actual token CONSUMPTION (`BrokerAdapter.submit`) -- see
those two docstrings. This module adds a THIRD checkpoint, deliberately
redundant with the first: checking `now - request.shown_at` against
`service.min_display` BEFORE ever calling `store.decide()`, so a premature
approve POST is refused WITHOUT spending this request's one decision (and
the daily approval cap it counts against) on an attempt that would fail to
mint anyway. If this pre-check ever drifted from `ApprovalService.approve`'s
own (it must not, and is deliberately the same comparison), that method's
own internal check is still the authoritative backstop -- this is layered
defense, not a second, competing source of truth for the SAME decision.

IDEMPOTENT BY request_id, NOT BY (request_id, action). A replayed identical
POST (approve after approve, reject after reject) returns the ORIGINAL
outcome rather than deciding or minting a second time. A POST for the
OTHER action against an already-decided request (approve after reject, or
vice versa) is a genuine conflict -- `DecisionConflict` -- not a replay,
and is refused.

MODIFY-WITHIN-BOUNDS FIELDS TRAVEL WITH THE APPROVE CALL AND ARE VERIFIED
SERVER-SIDE. `size_pct` (a percentage of the request's own `authorized_qty`,
must be in `(0, 100]`) and `limit_price` (must move adversely to the trade
relative to the request's own proposed limit) are converted into
`qty_override`/`limit_price_override` and handed to `agent.approval_bridge.
mint_approval_token`, which validates them against the request's own
`proposal_snapshot` and raises rather than clamping -- see that module's
own `_validate_modification`. Nothing here re-derives that bounds logic a
second time.
"""
from __future__ import annotations

from datetime import datetime

from .approval import ApprovalService, TokenReissued
from .approval_bridge import ApprovalBridgeError, mint_approval_token
from .approval_request_store import ApprovalRequestStore
from .audit import AuditLog


class DecisionError(Exception):
    """A refused decision -- unknown request, bad modify-within-bounds
    fields, friction not yet satisfied, or a bridge refusal. Callers map
    this to a 4xx response."""


class DecisionConflict(DecisionError):
    """The request is already decided the OTHER way -- a genuine conflict,
    not a replay of the same action."""


def _token_result(token, *, replayed: bool) -> dict:
    return {
        "token_id": token.token_id, "request_id": token.request_id,
        "expires_at": token.expires_at.isoformat(),
        "decision_elapsed_ms": token.decision_elapsed_ms,
        "price_band": list(token.price_band),
        "original_qty": token.original_qty,
        "original_limit_price": token.original_limit_price,
        "replayed": replayed,
    }


def approve(request_id: str, *, store: ApprovalRequestStore,
           service: ApprovalService, audit_log: AuditLog, now: datetime,
           actor: str, size_pct: float = 100.0,
           limit_price: float | None = None) -> dict:
    """Approve `request_id` and mint its token. See module docstring for
    the friction pre-check, the idempotency contract, and the
    modify-within-bounds fields. Raises `DecisionError`/`DecisionConflict`
    on refusal -- never raises `ApprovalError`/`ApprovalBridgeError`
    directly, so `agent.dashboard_server` has exactly one exception type
    to translate into a 4xx per endpoint."""
    request = store.get(request_id)
    if request is None:
        raise DecisionError(f"unknown request_id {request_id!r}")

    if request.decision == "REJECTED":
        raise DecisionConflict(
            f"request {request_id} was already REJECTED; cannot approve it"
        )

    if request.decision is None:
        # Friction, checked BEFORE this request's one decision is spent --
        # see module docstring.
        elapsed = now - request.shown_at
        if elapsed < service.min_display:
            raise DecisionError(
                f"card shown for {elapsed.total_seconds():.1f}s; minimum is "
                f"{service.min_display.total_seconds():.0f}s before approve "
                "is enabled (§10); refusing before deciding anything"
            )
        if not (0 < size_pct <= 100):
            raise DecisionError(f"size_pct must be in (0, 100], got {size_pct}")
        request = store.decide(request_id, decision="APPROVED", now=now,
                               decided_by=actor)
        audit_log.append(
            actor=actor, action="approval_request_decided",
            object_type="approval_request", object_id=request_id,
            before={"decision": None},
            after={"decision": "APPROVED", "size_pct": size_pct,
                  "limit_price": limit_price},
            timestamp=now,
        )
    else:
        # Already APPROVED -- a replay, or a decision made some other way
        # with no token minted yet. Either way, do not decide again; fall
        # through to (re)minting below using the EXISTING decision.
        existing_token = service.token_for_request(request_id)
        if existing_token is not None:
            return _token_result(existing_token, replayed=True)

    proposal = request.proposal_snapshot
    authorized_qty = proposal.get("authorized_qty")
    qty_override = (authorized_qty * size_pct / 100.0
                    if authorized_qty is not None else None)

    try:
        token = mint_approval_token(
            request_id, store=store, service=service, now=now,
            audit_log=audit_log, qty_override=qty_override,
            limit_price_override=limit_price,
        )
    except TokenReissued:
        # A concurrent/replayed call minted it between our own check above
        # and this call -- return the (now-existing) token rather than
        # treating a race as an error.
        existing = service.token_for_request(request_id)
        if existing is None:
            raise DecisionError(
                f"token for request {request_id} was reissued but cannot "
                "be found; this should be unreachable"
            )
        return _token_result(existing, replayed=True)
    except ApprovalBridgeError as exc:
        raise DecisionError(str(exc)) from exc

    return _token_result(token, replayed=False)


def _reject_result(request, *, replayed: bool) -> dict:
    return {
        "request_id": request.request_id, "decision": request.decision,
        "decided_at": request.decided_at.isoformat() if request.decided_at else None,
        "decision_elapsed_ms": request.decision_elapsed_ms,
        "replayed": replayed,
    }


def reject(request_id: str, *, store: ApprovalRequestStore, audit_log: AuditLog,
          now: datetime, actor: str) -> dict:
    """Reject `request_id` via `ApprovalRequestStore.decide`. Idempotent by
    request_id: a replayed reject of an already-REJECTED request returns
    the original decision. A reject against an already-APPROVED request is
    a conflict, not a replay."""
    request = store.get(request_id)
    if request is None:
        raise DecisionError(f"unknown request_id {request_id!r}")

    if request.decision == "APPROVED":
        raise DecisionConflict(
            f"request {request_id} was already APPROVED; cannot reject it"
        )
    if request.decision == "REJECTED":
        return _reject_result(request, replayed=True)

    request = store.decide(request_id, decision="REJECTED", now=now, decided_by=actor)
    audit_log.append(
        actor=actor, action="approval_request_decided",
        object_type="approval_request", object_id=request_id,
        before={"decision": None}, after={"decision": "REJECTED"}, timestamp=now,
    )
    return _reject_result(request, replayed=False)
