"""Submits an APPROVED `agent.entities.ApprovalRequest` -- the seam Unit 1
and Unit 2 exist to feed (Unit 3, 2026-08-09). Takes a request_id, the
request's own already-minted `agent.approval.ApprovalToken` (`agent.
approval_bridge.mint_approval_token` -- the only production caller of
`ApprovalService.approve`), and a `agent.broker.base.BrokerAdapter`; submits
the EXACT order the operator approved, or refuses and says why.

VERIFY, NEVER RE-DERIVE. `Gatekeeper.stage` is not called again here.
Calling it a second time would size against WHATEVER portfolio state exists
at submission time -- reserve, positions, other pending earmarks may all
have moved since the request was created -- silently producing a DIFFERENT
order than the one an operator actually saw and approved. `agent.
approval_trigger.staged_order_from_snapshot` (Unit 1) reconstructs the
EXACT, persisted `StagedOrder` instead; this module submits THAT, never a
freshly-staged substitute. `MissingStagedOrder` (a request that predates
Unit 1) propagates uncaught -- fails closed, exactly per that exception's
own docstring.

THE SIGNING KEY IS NOW DURABLE -- VERIFY THE PERSISTED SIGNATURE, NEVER
RE-SIGN (follow-up unit, 2026-08-09). `Gatekeeper.signing_key` used to be a
fresh random value per instance with no persistence anywhere in this
codebase (see Unit 1's own report, and the now-superseded paragraph this
one replaces in `agent.approval_trigger`'s module docstring); this module
used to work around that by RE-SIGNING the reconstructed order with the
executing process's own key before submitting. That was honest about the
gap but proved less than the design calls for: the signature ended up
proving only "this process just signed this content," never "the process
that ran the gates produced this exact order." `scripts/run_agent.py` now
resolves `Gatekeeper.signing_key` from a durable secret
(`agent.secrets_provider.SecretsProvider.resolve`, the SAME read-only
mechanism already used for broker API credentials) instead of generating
one per process -- see that script's own `_resolve_gatekeeper_signing_key`
and module docstring for exactly how an operator provisions it (by hand,
into the OS keychain; this codebase still never writes a secret to disk).
Two independently-constructed `Gatekeeper` instances that resolved the
SAME durable key now produce and verify the SAME signature, which is the
actual point of signing gate output in the first place.

Given that, this module VERIFIES the persisted signature and refuses on
failure -- `staged.verify(gatekeeper.signing_key)`, checked immediately
after `staged` is reconstructed, before any of its fields are trusted for
anything else (including `client_order_id`, used by the very next check).
A signature that does not verify is a HARD STOP: `StagingSignatureInvalid`,
uncaught by anything in this module, no fallback re-sign, no
warning-and-continue. `adapter.attach_staging_key(gatekeeper.signing_key)`
still runs before `submit()` -- a freshly-constructed adapter has no
staging key attached at all (see `attach_staging_key`'s own docstring) --
but the ORDER passed to `submit()` is the persisted `staged` object,
unmodified, not a re-signed copy.

CUTOVER: A REQUEST STAGED BEFORE THIS UNIT CANNOT EVER VERIFY. Its
signature was produced by a since-discarded, per-process random key that
the durable key cannot reproduce -- this is not a bug in the verify check,
it is the check doing exactly its job on genuinely unverifiable input.
`staged_order_from_snapshot` still reconstructs the (now known-untrustworthy)
`StagedOrder` fine; `.verify()` is what correctly refuses it, the same
fail-closed posture `MissingStagedOrder` already uses for a request that
predates Unit 1 entirely. THE OPERATOR REMEDY AT THE MOMENT OF CUTOVER: any
request that is DECIDED (approved) but not yet submitted when the durable
key is provisioned and the loop restarted can never be executed as-is --
`ApprovalRequestStore.decide()` already permanently refuses to re-decide an
approved request (Unit 2's own design), so there is no path to a fresh,
verifiable signature for that specific request. The operator invalidates it
(`ApprovalRequestStore.invalidate`, an existing method -- not new here) and
lets the underlying opportunity be re-screened and re-staged, which signs
with the now-durable key from that point on. A request still PENDING
(undecided) at cutover is unaffected either way -- nothing has been signed
for it yet.

NEVER RESUBMIT TO FIND OUT (`agent.broker.base.BrokerAdapter.
get_by_client_id`'s own docstring). Checked FIRST, before the token is ever
touched: if an order for `staged.client_order_id` already exists at the
broker, it is returned as-is -- no re-verification, no token consumption
attempt. This is the resolution mechanism for the genuine ambiguous-retry
case Unit 2's own report named as unsolved: `ApprovalToken.consume()` is
called BEFORE `_submit_impl` inside `BrokerAdapter.submit` (agent/
broker/base.py:510-520), so a submit that failed AFTER consuming the token
but before (or during) the broker call leaves the token permanently
unconsumable a second time -- retrying `submit()` with the same token would
raise `TokenConsumed`, never reaching `_submit_impl`'s own idempotency-by-
`client_order_id` promise. Checking `get_by_client_id` here, before ever
calling `submit()` again, means a retry after an ambiguous failure resolves
via the DURABLE, broker-side idempotency key -- exactly what that
docstring's "never resubmit to find out" already promises -- rather than
needing a new human decision (which `ApprovalRequestStore.decide()` would
refuse to grant anyway, per Unit 2's own design).

DRIFT CHECKS -- SUFFICIENCY, NOT RE-SIZING (invariant #1: risk is applied
to the target weight vector before any order exists, never per-order after
the fact). This module does not re-run `risk_constrain`, does not re-check
capability/holding/day-trade gates (those live in `Gatekeeper.stage`,
already run once, and are independently re-derived by `BrokerAdapter.
submit` itself from `staged`'s own fields -- gate 4, unchanged), and never
resizes the order. The only two checks here are SUFFICIENCY checks against
freshly-read broker state: for a BUY, does current `settled_cash` still
cover the approved `notional`; for a SELL/CLOSE, does the current held
qty for `symbol` still cover the approved `authorized_qty`. Either failing
means the world the approval was granted against has changed enough that
submitting the SAME order could not do what it was approved to do (an
underfunded buy, an over-large close) -- refused, not silently resized,
not silently re-staged.

PRICE BAND: NOT RE-DERIVED HERE EITHER. `ApprovalToken.consume` (called
inside `BrokerAdapter.submit`) already checks `reference_price` against the
token's own `price_band` -- the check this module would otherwise
duplicate. `reference_price` is a REQUIRED parameter of
`execute_approved_request`, supplied by this module's caller (the CLI,
which requires it as an explicit operator-supplied flag) -- this module
does not fetch a market quote itself; see this unit's own delivery report
for why (no market-data client is threaded through here, and inventing one
was judged out of scope for an operator-invoked, one-shot command).

NOT DONE HERE, ON PURPOSE (per this unit's own instructions): does not
build a dashboard route, does not wire into `agent.run_loop.run_loop` or
the unattended scheduled loop, and is operator-invoked only via
`scripts/run_agent.py --submit-approved` this unit.
"""
from __future__ import annotations

from .approval import ApprovalToken, verify_modification_within_bounds
from .approval_request_store import ApprovalRequestStore
from .approval_trigger import staged_order_from_snapshot
from .broker.base import BrokerAdapter, BrokerOrder
from .pipeline import Gatekeeper

# Same tolerance style as agent.approval_bridge's own qty-override guard
# (1e-9) -- floating-point notional/qty comparisons, not a real risk
# tolerance being loosened.
_DRIFT_EPSILON = 1e-6


class ExecutionError(Exception):
    pass


class DriftDetected(ExecutionError):
    """Current broker state no longer supports the exact, approved order --
    see this module's own "DRIFT CHECKS" docstring section."""


class StagingSignatureInvalid(ExecutionError):
    """The persisted `StagedOrder`'s signature does not verify against this
    `Gatekeeper`'s (now-durable) `signing_key`. A hard stop -- no fallback
    re-sign, no warning-and-continue (module docstring's CUTOVER section).
    The two real causes: (1) this request was staged before the durable
    signing key was provisioned/cut over, so its signature was produced by
    a since-discarded per-process random key that can never verify again --
    the operator remedy is to invalidate this request
    (`ApprovalRequestStore.invalidate`) and let it be re-staged; or (2) the
    persisted `proposal_snapshot` was altered since staging. Either way,
    nothing below this check may trust `staged`'s fields -- this is why it
    is raised immediately after reconstruction, before even
    `staged.client_order_id` is read."""


def execute_approved_request(
    request_id: str, *, store: ApprovalRequestStore, adapter: BrokerAdapter,
    gatekeeper: Gatekeeper, token: ApprovalToken, reference_price: float,
) -> BrokerOrder:
    """Verify the persisted `StagedOrder` against current broker state and
    submit it. No `now` parameter: `BrokerAdapter.submit` always derives
    "now" from `adapter.clock()` itself (never a caller-supplied value --
    see that method's own body), so a `now` accepted and threaded through
    here would be silently ignored at the one place it would matter,
    misleadingly implying a control this function does not actually have.

    Raises `ExecutionError` (or a subclass) on any refusal this module
    itself detects; propagates whatever `agent.approval_trigger.
    staged_order_from_snapshot`/`agent.broker.base.BrokerAdapter.submit`
    themselves raise unchanged (`MissingStagedOrder`, `MissingApproval`,
    `OrderMismatch`, `PriceOutOfBand`, `TokenExpired`, `TokenConsumed`,
    `CrossAccountError`, `StagingForged`, a capability `Rejected`, ...) --
    see module docstring for why none of those are re-implemented or
    swallowed here."""
    request = store.get(request_id)
    if request is None:
        raise ExecutionError(f"unknown request_id {request_id!r}")
    if request.decision != "APPROVED":
        raise ExecutionError(
            f"request {request_id} is not approved (decision="
            f"{request.decision!r}); refusing to execute"
        )
    if request.invalidated_reason is not None:
        raise ExecutionError(
            f"request {request_id} was invalidated "
            f"({request.invalidated_reason}); refusing to execute"
        )

    # Never re-derive -- reconstructed verbatim from what Unit 1 persisted
    # at stage time. Raises MissingStagedOrder, uncaught, for a pre-Unit-1
    # request (see module docstring).
    staged = staged_order_from_snapshot(request.proposal_snapshot)

    # VERIFY, NEVER RE-DERIVE OR RE-SIGN (module docstring). Checked
    # immediately, before ANY of staged's fields are trusted for anything
    # below -- including client_order_id, read by the very next check. A
    # signature that does not verify is a hard stop: no fallback re-sign,
    # no warning-and-continue. The most common real cause is a request
    # staged before the durable signing key was cut over (module
    # docstring's CUTOVER section).
    if not staged.verify(gatekeeper.signing_key):
        raise StagingSignatureInvalid(
            f"request {request_id}: the persisted StagedOrder's signature "
            "does not verify against this Gatekeeper's signing_key. Either "
            "this request was staged before the durable signing key was "
            "cut over (its signature was produced by a since-discarded "
            "per-process random key and can never verify again) or the "
            "persisted proposal_snapshot has been altered since staging. "
            "Refusing to re-sign or re-derive either way -- a pre-cutover "
            "request must be invalidated (ApprovalRequestStore.invalidate) "
            "and re-staged, never trusted as-is."
        )

    if token.request_id != request_id:
        raise ExecutionError(
            f"token {token.token_id} belongs to request {token.request_id!r}, "
            f"not {request_id!r}; refusing to execute"
        )

    # NEVER RESUBMIT TO FIND OUT (module docstring). Checked before the
    # token is touched at all -- an order already at the broker for this
    # client_order_id is returned as-is, resolving a genuine ambiguous-
    # submit retry without a fresh human decision or a second token spend.
    existing = adapter.get_by_client_id(staged.client_order_id)
    if existing is not None:
        return existing

    # DRIFT CHECKS -- sufficiency against freshly-read broker state, not a
    # re-sizing and not a re-run of any gate (module docstring).
    account = adapter.account()
    if staged.side == "BUY":
        available = float(account.settled_cash)
        if available < staged.notional - _DRIFT_EPSILON:
            raise DriftDetected(
                f"request {request_id}: approved notional {staged.notional} "
                f"for {staged.symbol} exceeds current settled cash "
                f"{available}; refusing to submit a since-underfunded order"
            )
    elif staged.side in ("SELL", "CLOSE"):
        positions = {p.symbol: float(p.qty) for p in adapter.positions()}
        held = positions.get(staged.symbol, 0.0)
        if held < staged.authorized_qty - _DRIFT_EPSILON:
            raise DriftDetected(
                f"request {request_id}: approved qty {staged.authorized_qty} "
                f"of {staged.symbol} exceeds current held qty {held}; "
                "refusing to submit a since-over-large close"
            )

    # `verify_modification_within_bounds` also runs INSIDE submit() against
    # `staged`'s own fields -- called again here, explicitly, only so a
    # drift between the persisted StagedOrder and the token's own approved
    # fields is caught with THIS module's own error, before ever touching
    # the adapter.
    verify_modification_within_bounds(
        token, symbol=staged.symbol, side=staged.side, qty=staged.authorized_qty,
        order_type=staged.order_type, time_in_force=staged.time_in_force,
        limit_price=staged.limit_price, lot_id=staged.lot_id,
    )

    # Wire this adapter to the SAME (durable) key the signature above was
    # verified against -- a freshly-constructed adapter has no staging key
    # attached at all (see `attach_staging_key`'s own docstring); this is
    # not a re-sign, `staged` itself is submitted unmodified below.
    adapter.attach_staging_key(gatekeeper.signing_key)

    return adapter.submit(staged, approval_token=token, reference_price=reference_price)
