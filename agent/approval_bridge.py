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

SHOWN_AT AGREEMENT IS A TOLERANCE, NOT AN EQUALITY (review fix, 2026-08-02).
This bridge always passes the STORE's own `request.shown_at` into
`ApprovalService.approve` -- never a value this function's own caller
supplies -- and always passes the STORE's own already-audited
`request.decision_elapsed_ms` THROUGH as the token's authoritative figure,
rather than letting `approve()` recompute one from `now - shown_at` at
mint time. An earlier version of this function REQUIRED the recomputed
figure to EXACTLY equal the stored one before it would even call
`approve()` -- but any real caller calls `decide()`, then mints with a
SECOND, independent `datetime.now()` read a few microseconds or
milliseconds later; those two reads are never bit-for-bit equal, so that
check could never survive a real caller. The intent survives as a SANITY
BOUND instead (`SHOWN_AT_DRIFT_TOLERANCE_MS` below): this function still
computes what `approve()` would compute from `now - request.shown_at`, and
still raises BEFORE ever calling `approve()` (so a rejected attempt never
burns the request's one `token_id`) -- but only if that recomputed figure
is NEGATIVE (a `now` before `shown_at` -- impossible for a genuine mint)
or exceeds the store's own recorded `decision_elapsed_ms` by more than the
tolerance (a `shown_at` genuinely wrong, or a caller passing a `now` from
some unrelated, much later action -- not clock jitter between two reads of
the same real approval).

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

PRICE BAND IS INHERITED FROM THE REQUEST, NOT RECOMPUTED (review fix,
2026-08-02). This function always passes `request.price_band_low`/
`request.price_band_high` into `ApprovalService.approve` -- the exact band
the operator saw on the card -- rather than letting `approve()` derive one
fresh from `price_at_analysis`/its own `price_band_pct` at mint time.
`ApprovalService.approve` prefers the stored band and audits any
disagreement with what it would otherwise compute (see that method's own
docstring); this function supplies `audit_log` through to it so that trace
actually gets written when this bridge is the caller.

MODIFY-WITHIN-BOUNDS AT APPROVAL TIME (§10; operator decision surface unit,
2026-08-03). An operator may approve a SMALLER size, or a limit that moves
ADVERSELY to the trade, without a fresh analysis -- the same §10 rule
`agent.approval.verify_modification_within_bounds` already enforces at
TOKEN CONSUMPTION, applied here at MINT time instead, against the
REQUEST's own `proposal_snapshot` (there is no token yet to compare
against). `qty_override`/`limit_price_override`, both optional, are
validated by `_validate_modification` below before the fingerprint is even
computed: a size larger than what was authorized, or a limit that moves
FAVOURABLY (higher for a BUY, lower for a SELL/CLOSE), raises
`ApprovalBridgeError` rather than silently clamping or minting for the
unmodified order. On success, the OVERRIDE values -- not the snapshot's
own -- are what get fingerprinted and passed to `ApprovalService.approve`,
so the resulting token is bound to the order the operator actually
authorized, not the one T4 originally proposed. This mirrors `verify_
modification_within_bounds`'s own bounds logic deliberately (kept as a
short, independent check here rather than manufacturing a fake token just
to reuse that function) -- if the two ever need to diverge, that is itself
a finding worth raising, not a refactor to do quietly.

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

A DECIDED REQUEST MINTS EXACTLY ONE SPENDABLE TOKEN, DURABLY (Unit 2,
2026-08-09). Before this unit, `ApprovalService.approve`'s deterministic
`token_id` (`f"tok-{request_id}"`) plus its `TokenReissued` guard only
protected a SECOND mint attempt within the SAME `ApprovalService`
instance -- its `_tokens` dict is in-memory only. A fresh instance (a real
process restart; trivially, a second instance in a test) starts with an
empty dict, so a second call to this function for the same, already-
approved request_id would sail past `TokenReissued` entirely and mint a
second, fully independent `ApprovalToken` object -- unrelated to the
first, with its own `consumed_at=None` regardless of whether the first was
already spent. This function now checks `request.token_snapshot`
(`ApprovalRequestStore.record_token_minted`, durable, checked BEFORE ever
calling `service.approve()` again) and, if present, reconstructs and
returns THAT token verbatim -- no new mint, no re-validation of
`qty_override`/`limit_price_override` (a replay returns the ORIGINAL
decision, exactly as `agent.dashboard_decisions.approve`'s own "already
APPROVED" branch already does one layer up), regardless of how many
processes or `ApprovalService` instances have come and gone since the
original mint.

WHY "RETURN THE EXISTING TOKEN," NOT "REFUSE OUTRIGHT" (Unit 2 item 1's
explicit design choice). Refusing a second mint attempt outright would
strand the genuine retry case (item 3): a caller that already decided the
request but never learned whether the FIRST mint attempt succeeded (lost
response, process died between `service.approve()` returning and its
caller persisting anything -- see below) would have no way forward except
a brand new human decision, and `ApprovalRequestStore.decide()` already
permanently refuses to re-decide a request that is APPROVED. Returning the
existing token instead makes the retry resolvable by the ORIGINAL caller
retrying the SAME call, no new approval required.

CONSUMPTION IS STILL NOT DURABLE -- A DISCLOSED, UNCLOSED GAP. This unit
persists the token's MINT-time material (`_encode_token` below) -- never
its `consumed_at`/`swept_at`, because those are set later by `agent.
approval.ApprovalToken.consume()`, called from `agent.broker.base.
BrokerAdapter.submit()` (the execution path, out of scope this unit: see
Unit 2's own instructions). A token reconstructed from `token_snapshot`
after a real process restart therefore always reports `consumed_at=None`
and `swept_at=None`, even if the ORIGINAL in-memory token object was
already consumed before the restart. Within one process, this is not
reachable -- `ApprovalService.token_for_request` (checked by `agent.
dashboard_decisions.approve` before this bridge is ever called) already
returns the SAME, still-mutated object, so a consumed token's true state
is visible there. ACROSS a restart, nothing in this codebase durably
records that a token was spent, independent of whether an order resulted
-- closing that requires threading persistence through `consume()` itself,
which this unit does not do. See this unit's own delivery report for why
that is deliberately left for whatever unit next touches the execution
path (also review `_submit_impl`'s own idempotency-by-client_order_id
promise, which is the one existing durable defense against a double-
consumed reconstructed token producing a double-submitted order today).

SUPERSEDED (durable-consumption unit, 2026-08-09). The gap this section describes is closed.
`agent.broker.base.BrokerAdapter.submit()` now accepts an optional
consumption sink (`attach_token_consumption_sink`) that it calls
immediately after `ApprovalToken.consume()` succeeds and before the broker
is ever contacted; `agent.approval_execution.execute_approved_request`
wires a real one, backed by `agent.approval_request_store.
ApprovalRequestStore.record_token_consumed`, unconditionally. That call
REPLACES the request's `token_snapshot` with the token's post-consumption
state, so `_token_from_snapshot` (below), reached via this function's own
`request.token_snapshot` fallback for a fresh `ApprovalService` instance,
now reconstructs `consumed_at` correctly instead of always `None`. `_encode_
token` was renamed to `encode_token` (public) the same commit, so `agent.
approval_execution` can call it directly to build the sink's closure. See
that unit's own delivery report for the full "which way does a mid-submit
crash resolve" reasoning -- the paragraphs above are kept, uncorrected in
place, as the record of what was true before this unit, per this
codebase's own convention for a disclosed gap that is later closed.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from .approval import ApprovalService, ApprovalToken, order_fingerprint
from .approval_request_store import ApprovalRequestStore
from .audit import AuditLog

# Review fix, 2026-08-02: a generous bound for wall-clock jitter between the
# decide() call and this mint call (two separate datetime.now() reads on any
# real caller) -- NOT a real signal that anything is wrong, and not meant to
# be tight. A shown_at genuinely off by more than this (an hour, a day, a
# caller passing `now` from some unrelated action) is a real defect worth
# refusing to mint over; five seconds of scheduling/IO jitter between two
# calls that are supposed to represent the same real-world "operator
# approved this" instant is not.
SHOWN_AT_DRIFT_TOLERANCE_MS = 5000


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


def _validate_modification(proposal: dict, *, qty_override: float | None,
                           limit_price_override: float | None) -> None:
    """§10 modify-within-bounds, checked against the REQUEST's own
    `proposal_snapshot` (see module docstring). Raises `ApprovalBridgeError`
    -- never clamps -- on a size increase or a favourable limit move."""
    if qty_override is not None:
        authorized_qty = proposal["authorized_qty"]
        if qty_override <= 0:
            raise ApprovalBridgeError(
                f"qty_override {qty_override} must be positive"
            )
        if qty_override > authorized_qty + 1e-9:
            raise ApprovalBridgeError(
                f"qty_override {qty_override} exceeds the authorized qty "
                f"{authorized_qty}; size may only be reduced without "
                "re-analysis (§10)"
            )
    if limit_price_override is not None:
        original_limit = proposal.get("limit_price")
        if original_limit is None:
            raise ApprovalBridgeError(
                "limit_price_override was given but the approved order has "
                "no limit_price to modify"
            )
        side = proposal["side"].upper()
        if side == "BUY":
            if limit_price_override > original_limit + 1e-9:
                raise ApprovalBridgeError(
                    f"limit_price_override {limit_price_override} is above "
                    f"the approved {original_limit}; a BUY limit may only "
                    "move down (adversely to the trade) without re-analysis (§10)"
                )
        else:
            if limit_price_override < original_limit - 1e-9:
                raise ApprovalBridgeError(
                    f"limit_price_override {limit_price_override} is below "
                    f"the approved {original_limit}; a SELL/CLOSE limit may "
                    "only move up (adversely to the trade) without "
                    "re-analysis (§10)"
                )


_TOKEN_FIELDS = (
    "token_id", "request_id", "order_fingerprint", "price_band", "expires_at",
    "decided_at", "decision_elapsed_ms", "original_symbol", "original_side",
    "original_qty", "original_order_type", "original_time_in_force",
    "original_limit_price", "original_lot_id", "shown_at", "min_display",
    "consumed_at", "swept_at",
)


def encode_token(token: ApprovalToken) -> dict:
    """Every current `ApprovalToken` field, verbatim -- see this module's
    own "A DECIDED REQUEST MINTS EXACTLY ONE SPENDABLE TOKEN" docstring
    section for the mint-time use, and "SUPERSEDED (durable-consumption unit, 2026-08-09)" for
    the consumption-time one. RENAMED from `_encode_token` (durable-consumption unit) --
    public because `agent.approval_execution.execute_approved_request` now
    calls it directly to build `record_token_consumed`'s closure, not just
    this module's own `mint_approval_token`. At mint time, `consumed_at`/
    `swept_at` are always `None` (a token is encoded immediately after
    `service.approve()` mints it, before anything could have consumed or
    swept it); at consumption time (the new caller) `consumed_at` is
    whatever `ApprovalToken.consume()` just set. Encoded verbatim either
    way, rather than hardcoded, so this function has exactly one job
    (serialize whatever the token currently says) and no implicit
    assumption about when it runs."""
    encoded = {name: getattr(token, name) for name in _TOKEN_FIELDS}
    encoded["price_band"] = list(encoded["price_band"])
    encoded["expires_at"] = encoded["expires_at"].isoformat()
    encoded["decided_at"] = encoded["decided_at"].isoformat()
    encoded["shown_at"] = encoded["shown_at"].isoformat() if encoded["shown_at"] else None
    encoded["min_display"] = (encoded["min_display"].total_seconds()
                              if encoded["min_display"] is not None else None)
    encoded["consumed_at"] = encoded["consumed_at"].isoformat() if encoded["consumed_at"] else None
    encoded["swept_at"] = encoded["swept_at"].isoformat() if encoded["swept_at"] else None
    return encoded


def _token_from_snapshot(snapshot: dict) -> ApprovalToken:
    """Reconstructs the token exactly as it was at MINT time -- see this
    module's own "CONSUMPTION IS STILL NOT DURABLE" docstring section for
    why `consumed_at`/`swept_at` reconstruct as whatever was in the
    snapshot (always `None`, today) rather than reflecting any consumption
    that happened to the original in-memory object after mint."""
    fields = dict(snapshot)
    fields["price_band"] = tuple(fields["price_band"])
    fields["expires_at"] = datetime.fromisoformat(fields["expires_at"])
    fields["decided_at"] = datetime.fromisoformat(fields["decided_at"])
    fields["shown_at"] = (datetime.fromisoformat(fields["shown_at"])
                          if fields["shown_at"] else None)
    fields["min_display"] = (timedelta(seconds=fields["min_display"])
                             if fields["min_display"] is not None else None)
    fields["consumed_at"] = (datetime.fromisoformat(fields["consumed_at"])
                             if fields["consumed_at"] else None)
    fields["swept_at"] = (datetime.fromisoformat(fields["swept_at"])
                          if fields["swept_at"] else None)
    return ApprovalToken(**fields)


def mint_approval_token(request_id: str, *, store: ApprovalRequestStore,
                        service: ApprovalService, now: datetime,
                        audit_log: AuditLog | None = None,
                        qty_override: float | None = None,
                        limit_price_override: float | None = None) -> ApprovalToken:
    """Given a request_id and the store, mint the corresponding token (or
    raise `ApprovalBridgeError`). See module docstring for the guard order,
    the real-field passthrough, the shown_at-agreement tolerance, the
    inherited price band, and the deterministic token_id. The ONLY
    production caller of `ApprovalService.approve`.

    `audit_log`, when supplied, is passed straight through to `approve()`
    so a price-band disagreement between what this request stored and what
    `service` would compute today gets an audit row (see `ApprovalService.
    approve`'s own docstring) -- optional because, like `service` itself in
    `agent.approval_trigger.request_approval_for_analysis`, no real caller
    in this codebase constructs this bridge yet (there is no operator
    decision surface to call it from -- see module docstring)."""
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

    # DURABLE REPLAY (Unit 2, 2026-08-09): a token was already minted for
    # this request -- possibly by a DIFFERENT `ApprovalService` instance
    # (a real restart; see module docstring's "A DECIDED REQUEST MINTS
    # EXACTLY ONE SPENDABLE TOKEN" section). Return it verbatim. Checked
    # BEFORE the request's own `expires_at` deliberately: a request's
    # expiry governs whether a NEW decision may still act on it, not
    # whether an ALREADY-minted token may still be handed back to whoever
    # is asking for it again -- the token carries its own, independent
    # `expires_at`, unaffected by this. No re-validation of `qty_override`/
    # `limit_price_override` against a replay -- this returns the ORIGINAL
    # decision, exactly as `agent.dashboard_decisions.approve`'s own
    # "already APPROVED" branch already does for its own in-memory fast
    # path one layer up.
    #
    # THE IN-MEMORY OBJECT FIRST, WHEN THIS `service` STILL HOLDS IT -- only
    # falling back to reconstructing from the durable snapshot when it does
    # not (a fresh instance; see module docstring's "CONSUMPTION IS STILL
    # NOT DURABLE" section). Checking the durable snapshot FIRST, unconditionally,
    # would silently discard any consumption that happened to THIS SAME
    # process's own live token object -- reconstructing a fresh copy with
    # `consumed_at=None` even when `service` itself knows better.
    live = service.token_for_request(request_id)
    if live is not None:
        return live
    if request.token_snapshot is not None:
        return _token_from_snapshot(request.token_snapshot)

    if now >= request.expires_at:
        raise ApprovalBridgeError(
            f"request {request_id} expired at {request.expires_at.isoformat()}; "
            "refusing to mint a token"
        )

    proposal = request.proposal_snapshot
    _reject_compound_snapshot(proposal)
    _validate_modification(proposal, qty_override=qty_override,
                           limit_price_override=limit_price_override)

    # The store's own recorded shown_at, ALWAYS -- never a value this
    # function's own caller supplies (see module docstring). The sanity
    # bound below is computed BEFORE calling approve(), so a rejected
    # attempt is caught before this request's one token_id is ever consumed
    # by ApprovalService -- a retry with a corrected `now` would otherwise
    # hit TokenReissued for a token that was never actually minted
    # successfully the first time.
    would_be_elapsed_ms = int((now - request.shown_at).total_seconds() * 1000)
    if would_be_elapsed_ms < 0:
        raise ApprovalBridgeError(
            f"negative elapsed for request {request_id}: this call's own "
            f"`now` ({now.isoformat()}) is BEFORE the store's own shown_at "
            f"({request.shown_at.isoformat()}); refusing to mint a token"
        )
    if would_be_elapsed_ms > request.decision_elapsed_ms + SHOWN_AT_DRIFT_TOLERANCE_MS:
        raise ApprovalBridgeError(
            f"shown_at drift for request {request_id} exceeds the "
            f"{SHOWN_AT_DRIFT_TOLERANCE_MS}ms tolerance: this call's own "
            f"`now` would compute {would_be_elapsed_ms}ms from the store's "
            f"shown_at, but the store recorded "
            f"{request.decision_elapsed_ms}ms at decide() time. Pass a "
            "`now` close to the one used for the decide() call that "
            "approved this request."
        )

    # The actually-authorized order: the override when one was given and
    # validated above, else the proposal's own value unchanged.
    final_qty = qty_override if qty_override is not None else proposal["authorized_qty"]
    final_limit_price = (limit_price_override if limit_price_override is not None
                         else proposal.get("limit_price"))

    token_id = f"tok-{request_id}"
    fingerprint = order_fingerprint(
        symbol=proposal["symbol"], side=proposal["side"],
        qty=final_qty, order_type=proposal["order_type"],
        time_in_force=proposal["time_in_force"],
        limit_price=final_limit_price, lot_id=proposal.get("lot_id"),
    )
    tok = service.approve(
        token_id=token_id, request_id=request_id, fingerprint=fingerprint,
        price_at_analysis=request.price_at_analysis, shown_at=request.shown_at,
        now=now, symbol=proposal["symbol"], side=proposal["side"],
        qty=final_qty, order_type=proposal["order_type"],
        time_in_force=proposal["time_in_force"],
        limit_price=final_limit_price, lot_id=proposal.get("lot_id"),
        decision_elapsed_ms=request.decision_elapsed_ms,
        price_band_low=request.price_band_low, price_band_high=request.price_band_high,
        audit_log=audit_log,
    )
    # DURABLE, IMMEDIATELY (Unit 2): persisted before this function returns,
    # so the VERY NEXT call for this request_id -- same process or a fresh
    # one -- finds it via the `request.token_snapshot` check above instead
    # of reaching `service.approve()` a second time.
    store.record_token_minted(request_id, token_snapshot=encode_token(tok), now=now)
    return tok
