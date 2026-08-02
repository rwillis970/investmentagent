"""Bridge (bridge unit, 2026-08-02): connects an APPROVED `agent.entities.
ApprovalRequest` to a mintable `agent.approval.ApprovalToken`. Its own
module, not bolted onto either `agent.approval_request_store.
ApprovalRequestStore` or `agent.approval.ApprovalService` -- before this
unit, `ApprovalRequestStore.decide()` recorded a decision and `ApprovalService.
approve()` minted a token, and nothing joined them: no enforced request_id
linkage, and each computed its own `decision_elapsed_ms` from a DIFFERENT
`shown_at` (store-recorded vs. whatever a caller of `approve()` happened to
supply). Nothing in this codebase could act on an approved request before
this module.

WHY A SEPARATE MODULE, NOT A METHOD ON EITHER CLASS. `ApprovalRequestStore`
knows nothing about tokens; `ApprovalService` knows nothing about requests
beyond the bare kwargs `approve()` already accepts. Neither class's own
invariants (append-only replay for the store; single-use token issuance for
the service) need to change to add this join -- adding it as a THIRD, thin
module that reads from one and writes to the other keeps each class's own
existing tests and reasoning untouched.

GUARD ORDER (§9, §10) -- refuse unless the request's decision is exactly
"APPROVED" (a REJECTED or still-undecided request has no order to mint a
token for), it is not invalidated, and it is not past its OWN `expires_at`
(a request can be decided APPROVED and still be too stale to act on if the
caller is slow -- the REQUEST's `expires_at` governs how long the decision
itself remains actionable, entirely separate from the TOKEN's own, freshly
computed `expires_at` inside `ApprovalService.approve`). NOTE, checked by
grep before writing this module: `ApprovalRequestStore.decide()` refuses to
decide an already-invalidated request, and `.invalidate()` refuses to
invalidate an already-decided one -- so a request reached through this
store's own public API can never simultaneously be `decision == "APPROVED"`
and `invalidated_reason is not None`. The "not invalidated" guard here is
therefore presently unreachable via that API; it is kept anyway as
defense-in-depth against a request reached some other way (a directly
constructed object, a future relaxation of that mutual exclusion, or a
tampered/replayed row) -- see this unit's own report.

REAL ORDER FIELDS, NEVER THE DEFAULTS (§10's modify-within-bounds rule).
`ApprovalService.approve`'s `symbol`/`side`/`qty`/`order_type`/
`time_in_force`/`limit_price`/`lot_id` kwargs default to ""/0.0/None for
backward compatibility with every pre-existing test call in this codebase
that only ever exercised the exact-fingerprint path -- but `agent.approval.
verify_modification_within_bounds` compares a MODIFIED order's fields
against `token.original_*`, and a symbol of `""`/a qty of `0.0` can never
equal any real order's own fields. A token minted through this bridge
without its request's real proposal fields would be a token no order --
not even the exact one that was approved -- could ever spend through the
modify-within-bounds path. This module always reads those fields from the
request's own `proposal_snapshot` (the actual approved order,
`agent.approval_trigger.request_approval_for_analysis`'s own output) and
passes them straight through; see this unit's own tests for the explicit
"defaults can never succeed" assertion.

SHOWN_AT AGREEMENT, NOT A SECOND GUESS. This bridge always passes the
STORE's own `request.shown_at` into `ApprovalService.approve` -- never a
value this function's own caller supplies -- so `ApprovalService.approve`'s
`now - shown_at` computation and `ApprovalRequestStore.decide`'s
already-recorded `request.decision_elapsed_ms` are computed from the
IDENTICAL `shown_at`. They can still disagree if THIS function's own `now`
differs from whatever `now` the caller passed to the earlier `decide()`
call -- the natural calling convention is one shared `now` for the single
real-world action "an operator approved this," spanning both the `decide()`
call and this mint call. Rather than silently pick one figure over the
other (which would let a token's own `decision_elapsed_ms` drift from the
store's already-audited record of how long the card was actually on
screen), this function computes what `approve()` is ABOUT to compute,
compares it against the store's own recorded figure FIRST, and raises
BEFORE ever calling `approve()` -- so a caller that got `now` wrong never
mints a token at all (and never burns the request's one `token_id`); it can
simply retry with the correct `now`.

TOKEN_ID IS DERIVED, NEVER CALLER-SUPPLIED (idempotent replay). `f"tok-
{request_id}"` -- a request_id is already unique (`ApprovalRequestStore.
create` refuses a duplicate), so no hash is needed for uniqueness; this
scheme is deliberately simple and human-readable rather than opaque. A
second mint attempt for the same request_id (a replayed decision event, a
retried request after a transient failure) computes the IDENTICAL token_id
and therefore hits `ApprovalService`'s own existing `TokenReissued` guard
rather than silently minting a second, independently-live token for one
decision.

THE ONLY PRODUCTION CALLER OF `ApprovalService.approve` -- grepped before
writing this module (a plain-text search for "approve(" across agent/,
scripts/, and tests/): every
existing call site is in `tests/` (`test_broker_and_audit.py`,
`test_approval_token.py`, `test_startup.py`). There is no production caller
anywhere in this codebase today, because nothing yet calls
`ApprovalRequestStore.decide()` in production either -- the operator-facing
decision surface (the thing that would actually call `decide()` and then
this bridge) is not built in this codebase yet. This module is what that
future surface will call; it does not build that surface itself.

COMPOUND/MULTI-LEG REQUESTS ARE REFUSED, NOT SILENTLY MINTED FOR THE FIRST
LEG (§7 cash-account settlement). A sell-to-fund-buy cannot be one approval
in a cash account: the SELL's proceeds are unsettled for T+1, and
`UNSETTLED_CASH` funding is a DISABLED capability (`agent.pipeline.
Gatekeeper`'s own capability gate) -- financing a same-day BUY against a
same-day SELL's still-unsettled proceeds is not a missing feature to build
later, it is a settlement fact that makes "one approval, two legs"
incoherent for this account type. See `_reject_compound_snapshot` below.
Every REAL producer of `proposal_snapshot` in this codebase
(`agent.approval_trigger.request_approval_for_analysis`) only ever proposes
ONE order for one symbol (BUY xor CLOSE; confirmed by reading that module),
so this guard is defensive -- it exists so a FUTURE producer cannot
silently introduce a compound proposal through this bridge without
confronting this constraint, not a fix for an existing bug.

EARMARK HANDOFF (item 2). This module does not itself move an earmark
anywhere -- there is nothing to move. The earmark lives on the REQUEST
(`ApprovalRequest.earmark`, unchanged by this unit) for the request's
entire life; what changes is which of two queries -- `ApprovalRequestStore.
pending()` (this request, while still undecided) or `ApprovalService.
token_for_request()` (this request's token, once minted, while not yet
consumed/expired/swept) -- currently "sees" it as outstanding. See
`ApprovalRequestStore.outstanding_earmarks`'s own updated docstring (same
commit) for the mechanics. This bridge's only relevant obligation is that
it never itself consumes or sweeps the token -- minting is not spending.
"""
from __future__ import annotations

from datetime import datetime

from .approval import ApprovalService, ApprovalToken, order_fingerprint
from .approval_request_store import ApprovalRequestStore


class ApprovalBridgeError(Exception):
    pass


def _reject_compound_snapshot(proposal_snapshot: dict) -> None:
    """§7: a cash account cannot fund a same-day BUY from a same-day SELL's
    proceeds -- proceeds are unsettled for T+1 and `UNSETTLED_CASH` funding
    is DISABLED (`agent.pipeline.Gatekeeper`'s own capability gate). A
    `proposal_snapshot` naming more than one leg (a `"legs"` list) is refused
    outright rather than minting a token for only its first leg, which would
    silently authorize only half of what was actually proposed under one
    approval. No real producer in this codebase emits `"legs"` today (see
    module docstring) -- this is a defensive contract for whatever proposes
    a compound order next, not a fix for an existing caller. A snapshot with
    no `"legs"` key at all, or `"legs"` of length exactly one, is today's
    single-order shape and is not affected."""
    legs = proposal_snapshot.get("legs")
    if legs is not None and len(legs) != 1:
        raise ApprovalBridgeError(
            f"proposal_snapshot names {len(legs)} legs; a single cash-account "
            "approval cannot fund a compound order (e.g. a sell-to-fund-buy) "
            "-- proceeds are unsettled for T+1 and UNSETTLED_CASH funding is "
            "DISABLED (§7). This is a settlement fact, not a missing feature: "
            "split into separate approvals, each fundable from settled cash "
            "alone."
        )


def mint_approval_token(request_id: str, *, store: ApprovalRequestStore,
                        service: ApprovalService, now: datetime) -> ApprovalToken:
    """Given a request_id and the store, mint the corresponding token (or
    raise `ApprovalBridgeError`). See module docstring for the guard order,
    the real-field passthrough, the shown_at-agreement check, and the
    deterministic token_id. The ONLY production caller of
    `ApprovalService.approve`."""
    request = store.get(request_id)
    if request is None:
        raise ApprovalBridgeError(f"unknown request_id {request_id!r}")
    if request.decision != "APPROVED":
        raise ApprovalBridgeError(
            f"request {request_id} is not approved (decision="
            f"{request.decision!r}); refusing to mint a token"
        )
    if request.invalidated_reason is not None:
        raise ApprovalBridgeError(
            f"request {request_id} was invalidated "
            f"({request.invalidated_reason}); refusing to mint a token"
        )
    if now >= request.expires_at:
        raise ApprovalBridgeError(
            f"request {request_id} expired at {request.expires_at.isoformat()}; "
            "refusing to mint a token"
        )

    proposal = request.proposal_snapshot
    _reject_compound_snapshot(proposal)

    # The store's own recorded shown_at, ALWAYS -- never a value this
    # function's own caller supplies (see module docstring). Computed here,
    # BEFORE calling approve(), so a disagreement is caught before this
    # request's one token_id is ever consumed by ApprovalService -- a retry
    # with a corrected `now` would otherwise hit TokenReissued for a token
    # that was never actually minted successfully the first time.
    would_be_elapsed_ms = int((now - request.shown_at).total_seconds() * 1000)
    if would_be_elapsed_ms != request.decision_elapsed_ms:
        raise ApprovalBridgeError(
            f"decision_elapsed_ms disagreement for request {request_id}: "
            f"this call's own `now` would compute {would_be_elapsed_ms}ms "
            f"from the store's shown_at, but the store recorded "
            f"{request.decision_elapsed_ms}ms at decide() time. Pass the "
            "same `now` used for the decide() call that approved this "
            "request, rather than a later one."
        )

    token_id = f"tok-{request_id}"
    fingerprint = order_fingerprint(
        symbol=proposal["symbol"], side=proposal["side"],
        qty=proposal["authorized_qty"], order_type=proposal["order_type"],
        time_in_force=proposal["time_in_force"],
        limit_price=proposal.get("limit_price"), lot_id=proposal.get("lot_id"),
    )
    return service.approve(
        token_id=token_id, request_id=request_id, fingerprint=fingerprint,
        price_at_analysis=request.price_at_analysis, shown_at=request.shown_at,
        now=now, symbol=proposal["symbol"], side=proposal["side"],
        qty=proposal["authorized_qty"], order_type=proposal["order_type"],
        time_in_force=proposal["time_in_force"],
        limit_price=proposal.get("limit_price"), lot_id=proposal.get("lot_id"),
    )
