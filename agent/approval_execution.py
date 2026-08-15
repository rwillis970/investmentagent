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

MODE + RECONCILIATION GATE (security-remediation unit, 2026-08-15;
SAFETY-CRITICAL finding from the Codex Security full-repo scan of the
codex/admin-console-v1 branch -- treated as a production blocker
regardless of the scanner's own MEDIUM severity label, per explicit
instruction). THE GAP THIS CLOSES: `scripts/run_agent.py --submit-approved`
is a one-shot CLI dispatch (`_run_submit_approved`) that never calls
`agent.startup.run_startup` -- it builds its own `Gatekeeper`/adapter
inline and calls straight into `execute_approved_request` below, so NONE
of `run_startup`'s own mode-legality/reconciliation machinery ever ran on
this path. Before this unit, this function itself never consulted
`agent.mode_store.ModeStore` or `agent.runtime_status` AT ALL -- an
operator (or a script driving this CLI) with a persisted mode of PAUSED or
DISABLED could still reach the one real `adapter.submit(...)` call at the
bottom of this function, because nothing here ever asked what the
persisted mode was.

THE FIX LIVES HERE, NOT IN THE CLI, ON PURPOSE. A check added only to
`scripts/run_agent.py`'s dispatch would protect exactly one caller of this
function -- the scanner's own finding explicitly named this as
insufficient ("does not enforce authoritative ModeStore/fresh
reconciliation... Do not rely only on CLI startup paths"). `_mode_permits_
submission`/`_reconciliation_is_fresh` below are called from INSIDE
`execute_approved_request`, immediately adjacent to the sole `adapter.
submit(...)` call, alongside the existing SESSION GATE this unit's own
structure already established the pattern for -- so the invariant holds
for every current and future caller of this function, not merely
`_run_submit_approved`.

READ FRESH, FROM A PATH, NOT A PRE-BUILT OBJECT. `mode_store_path`/
`runtime_status_path` are STRINGS/PATHS, not an already-constructed
`ModeStore`/`RuntimeStatus` a caller might have built minutes (or, in a
long-running process, hours) earlier and could pass in stale without this
function ever knowing. `ModeStore(mode_store_path)` is constructed fresh,
right here, on every call (mirroring `ModeStore.__init__`'s own cheap
`_load()` -- this file is small and read-once-per-call, not cached across
calls); `runtime_status_module.read(runtime_status_path)` is likewise a
fresh disk read every time. A caller literally cannot defeat freshness by
holding onto a stale object, because this function never accepts one.

DEFAULT-DENY MODE ALLOWLIST, NOT A PAUSED/DISABLED BLOCKLIST. `_SUBMISSION_
PERMITTED_MODES = {"PAPER", "PRODUCTION_ACTIVE"}` -- submission is refused
for PAUSED, DISABLED, RESEARCH, an empty/never-written ModeStore
(`agent.mode.normalize_persisted(None)` == "DISABLED"), AND any value this
codebase does not even recognize as a mode at all (a corrupted file, a
future mode this function predates) -- an allowlist means an unexpected
value fails closed by construction, matching Appendix E's own "an unlisted
value is DISABLED" invariant, applied here to submission specifically
rather than capability checks.

RECONCILIATION FRESHNESS REUSES `agent.runtime_status`'s OWN DEFINITION,
NOT A NEW ONE. `runtime_status_module.is_stale(status, now=now)` (default
`DEFAULT_STALE_AFTER` = 25h) is the SAME shared staleness definition the
dashboard and `agent.phase_acceptance` already use -- this module adds a
caller, not a second, independently-drifting notion of "fresh." A missing
`runtime_status.json` (`read()` returns `None`), a `reconciliation_status`
that is not exactly `"PASS"` (WARN/FAIL/UNAVAILABLE all refuse), or a
present-but-stale snapshot are all treated identically: refuse, fail
closed, exactly this codebase's "fail safe to NO TRADE on any uncertainty
in data, broker state, policy or process health" invariant, applied to a
kind of uncertainty (recency of the last known-good reconciliation) this
function had never checked before.

TESTS PROVE ALL FOUR REQUIRED CASES LAND ON NO TRADE (`tests/test_approval_
execution.py`): PAUSED, DISABLED, a ModeStore that cannot be read/is
missing, and a stale-or-absent-or-non-PASS `runtime_status.json` -- in
every case, `adapter.submit` is asserted NEVER called (a scripted adapter
records calls; the assertion is on that record, not merely the raised
exception type) and the token remains unconsumed.

NEVER RESUBMIT TO FIND OUT -- NOW DEFENSE-IN-DEPTH, NOT THE MECHANISM
(`agent.broker.base.BrokerAdapter.get_by_client_id`'s own docstring;
demoted by the durable-consumption unit, 2026-08-09, item 3 of that unit's
own instructions: "keep the existing get_by_client_id() pre-check. It
stays as defence in depth, not as the mechanism"). Checked FIRST, before
the token is ever touched: if an order for `staged.client_order_id`
already exists at the broker, it is returned as-is -- no re-verification,
no token consumption attempt. Before the durable-consumption unit, this
was the ONLY mitigation for the ambiguous-retry case Unit 2's own report
named as unsolved: `ApprovalToken.consume()` is called BEFORE
`_submit_impl` inside `BrokerAdapter.submit` (agent/broker/base.py), so a
submit that failed AFTER consuming the token but before (or during) the
broker call left the token permanently unconsumable a second time --
retrying `submit()` with the same token raised `TokenConsumed`, never
reaching `_submit_impl`'s own idempotency-by-`client_order_id` promise.
That mitigation depended on the broker being reachable and answering
correctly -- a real gap, since this check itself calls the broker. The
durable-consumption unit closes the underlying gap independent of broker
reachability (see `adapter.attach_token_consumption_sink(...)` below and
`agent.broker.base.BrokerAdapter`'s own "DURABLE TOKEN CONSUMPTION"
docstring section): a token's consumed state is now known-spent, durably,
across a restart, without consulting the broker at all. This check is kept
UNCHANGED, still run FIRST, still exactly as valuable as before for the
one case it was always suited to (a genuine, already-placed order this
process lost track of) -- it answers "does an order already exist," a
different question than "was this approval already spent," and both
answers matter.

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

PRICE BAND: NOT RE-DERIVED HERE, BUT NO LONGER TRUSTED FROM THE CALLER
EITHER (security-remediation unit, 2026-08-15; MEDIUM finding, Codex
Security scan). `ApprovalToken.consume` (called inside `BrokerAdapter.
submit`) still does the actual band comparison -- this module still does
not duplicate that arithmetic. What changed: `reference_price` used to be
a REQUIRED parameter of `execute_approved_request`, supplied VERBATIM by
this module's caller (`scripts/run_agent.py --submit-approved-reference-
price`, a plain operator-typed float with no connection to a real market
feed) -- the exact gap the scan named: "a token may be consumed against a
caller-provided in-band value while the actual market order fills outside
the approved band," since nothing ever checked that the operator's typed
number bore any relationship to the real market. That parameter is GONE.
In its place, `quote_provider: QuoteProvider` (REQUIRED, no default) is
called for `staged.symbol` immediately before token consumption/
submission -- the SAME "read fresh, right before the one real submit
call" posture this unit's own MODE + RECONCILIATION GATE section already
established. `None`, a non-positive value, or any exception from
`quote_provider` is treated identically: `QuoteUnavailable`, fail closed,
no fallback to a stale or operator-guessed number. `scripts/run_agent.py`
wires a REAL implementation backed by `agent.broker.alpaca_market_data.
AlpacaMarketDataClient` (the same read-only Alpaca market-data client the
scheduled collector already uses) -- see that script's own `_alpaca_quote_
provider`. Tests inject a scripted/fake `quote_provider` instead of a
bare float, preserving deterministic testing (this unit's own explicit
instruction) without ever needing a real network call. MARKET ORDERS ARE
NOT INTRODUCED OR IMPLIED BY THIS FIX -- `staged.order_type` is unchanged,
still whatever `Gatekeeper.stage` decided; this fix only changes where the
price used for the BAND CHECK comes from, never what kind of order is
submitted (per this unit's own explicit instruction: "do not convert
market orders to something else merely to hide the issue" -- nothing here
converts anything; a LIMIT order stays a LIMIT order).

NOT DONE HERE, ON PURPOSE (per this unit's own instructions): does not
build a dashboard route, does not wire into `agent.run_loop.run_loop` or
the unattended scheduled loop, and is operator-invoked only via
`scripts/run_agent.py --submit-approved` this unit.

SESSION GATE (session-gate unit, 2026-08-13). Before this unit, NOTHING in
this function's own call path -- nor anywhere in `scripts/run_agent.py
--submit-approved` -- checked whether the account's permitted trading
session was open before calling `adapter.submit(...)`. `agent.run_loop.
run_loop` has always gated the SCHEDULED loop's own reconciliation/pipeline
cycle behind `in_session_now`, but `--submit-approved` is a completely
separate, operator-invoked CLI path that never went anywhere near that
gate -- an operator (or a script driving this CLI) could submit an
already-approved order at any hour, with only Alpaca's own, uninspected
acceptance behavior as a backstop. This was flagged as a disclosed defect
in the overnight-hardening unit's own final report (item 11) and is closed
here.

REUSED, NOT REIMPLEMENTED. `_session_permits_submission` below calls
`agent.run_loop.in_session_now` -- the SAME function `run_loop.run_loop`
already gates the scheduled loop with -- rather than defining a second,
independently-drifting notion of "market hours" in this module. There is
meant to be exactly one authoritative session definition in this codebase
(`agent.market_calendar`, via `agent.run_loop.in_session_now`); this
module adds a caller, not a competing implementation.

CHECKED IMMEDIATELY BEFORE THE ONLY CALL THAT CAN PLACE A REAL ORDER, NOT
AT APPROVAL-CREATION TIME. `agent.approval.ApprovalToken`'s own `shown_at`/
`expiration` already answer "was this approval created/shown recently
enough" -- a separate question from "is the session open RIGHT NOW, at the
literal instant of submission." A request can be approved during a live
session and then sit DECIDED for a while (an operator steps away, a script
queues it) before `--submit-approved` is actually run; checking the
session only once, back when the request was staged or approved, would
not catch that the world has since crossed into a closed session. The
check below runs as the LAST thing before `adapter.submit(...)` --
after every other gate in this function (signature verification,
idempotency, drift, `verify_modification_within_bounds`, staging-key/
token-consumption-sink wiring) -- so nothing about this fix skips, weakens,
or reorders any of them; it adds one more hard stop in front of the one
call that matters, using the SAME `adapter.clock()` instant `BrokerAdapter.
submit` itself derives `now` from immediately afterward (module docstring,
top: "No `now` parameter... a `now` accepted and threaded through here
would be silently ignored at the one place it would matter" -- this reuses
that same reasoning: this gate reads `adapter.clock()` fresh, it does not
invent its own clock).

FAIL CLOSED ON "CANNOT BE DETERMINED," NOT JUST ON "CLOSED."
`_session_permits_submission` treats ANY exception from `in_session_now`
(an out-of-range calendar date -- `agent.market_calendar.
CalendarCoverageError` -- a naive datetime, or anything else) the same way
it treats a genuinely closed session: refuse. This is this codebase's own
fail-safe-to-NO-TRADE invariant applied to a NEW kind of uncertainty
(session state), not a new invariant.

DOES NOT DEPEND ON THE BROKER TO ENFORCE THIS. Alpaca's own acceptance (or
rejection) of an order placed outside its accepted hours is a separate,
uninspected mechanism this codebase has never relied on for anything else
(see this module's own "PRICE BAND: NOT RE-DERIVED HERE EITHER" section
for the identical posture applied to price bands: this codebase decides
its own policy locally and does not lean on the broker's own enforcement
of a DIFFERENT policy to stand in for it) -- this gate runs and refuses
entirely locally, before the broker is ever contacted for this call.

TOKEN CONSUMPTION IS NOW DURABLE, WIRED HERE (durable-consumption unit,
2026-08-09). Immediately before `adapter.submit(...)` (alongside the
existing `attach_staging_key` call), this function now also calls
`adapter.attach_token_consumption_sink(...)` with a closure over `store.
record_token_consumed`. `BrokerAdapter.submit()` invokes that sink
immediately after its own in-memory `approval_token.consume(...)` succeeds
and BEFORE the broker is ever contacted (see that method's own docstring
for the exact placement argument) -- so by the time `_submit_impl` runs,
the fact that this specific approval has been spent is already fsynced to
`store`'s file, independent of whether the broker call that follows
succeeds, times out ambiguously, or never happens because this process
dies first.

WHICH WAY A MID-SUBMIT CRASH RESOLVES, AND WHY THAT DIRECTION IS SAFE. If
this process dies between the durable consume (the sink above) and the
broker actually accepting the order, the token is DURABLY, PERMANENTLY
consumed -- an order may or may not exist at the broker. On restart, this
function's caller re-derives a token via `agent.approval_bridge.
mint_approval_token`, which (per that module's own now-corrected
"CONSUMPTION IS STILL NOT DURABLE" section) reconstructs it FROM the
durable snapshot this sink just wrote, so the reconstructed copy already
reports the correct `consumed_at`. Two cases follow: (1) an order DOES
exist at the broker -- `get_by_client_id` (above, checked first, kept as
defense-in-depth) finds it and returns it as-is, no token touched a second
time; (2) an order does NOT exist -- `get_by_client_id` returns `None`,
this function falls through toward `adapter.submit(...)` again, and
`ApprovalToken.consume()` immediately raises `TokenConsumed` (its guard
order checks "already consumed" first) before the broker is ever
contacted a second time. That second case is a HARD STOP with no order
placed: the specific approval is permanently spent, unrecoverable, and the
operator must `ApprovalRequestStore.invalidate()` this request and let the
underlying opportunity be re-screened and re-staged -- the exact same
operator remedy the CUTOVER section above already prescribes for a
pre-cutover signature that can never verify. THIS IS THE SAFE DIRECTION:
the alternative (treating an uncertain mid-submit crash as "not yet
consumed," so a retry is free to try again) risks a genuine double-submit
if the first attempt's broker call actually landed but this process never
saw the acknowledgement -- exactly the failure Appendix E's fail-safe-to-
NO-TRADE bias exists to prevent. Losing one approval's worth of
opportunity to a hard stop is the acceptable cost; a duplicate live order
is not. This composes with, rather than conflicts with, the existing
ambiguous-submit retry path (item 3 of this unit's own instructions,
`get_by_client_id`, above): the two mechanisms answer different questions
-- broker-truth ("does an order exist") and authorization-truth ("was this
approval spent") -- and a caller needs both, checked in that order, to
resolve every case safely. No conflict was found between durable
consumption and that retry path; the "stop and report a conflict rather
than resolving it yourself" escape hatch this unit's instructions offered
was not needed.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Callable

from . import mode as mode_module
from . import runtime_status as runtime_status_module
from .approval import ApprovalToken, verify_modification_within_bounds
from .approval_bridge import encode_token
from .approval_request_store import ApprovalRequestStore
from .approval_trigger import staged_order_from_snapshot
from .broker.base import BrokerAdapter, BrokerOrder
from .mode_store import ModeStore
from .pipeline import Gatekeeper
from .run_loop import in_session_now

# A fresh, independently-obtained price for one symbol, or None if
# unavailable -- see module docstring's "PRICE BAND" section
# (security-remediation unit, 2026-08-15). Any exception raised by a real
# implementation is treated by `execute_approved_request` the same as a
# `None` return: fail closed, never fall back to a caller-supplied value.
QuoteProvider = Callable[[str], "float | None"]

# Same tolerance style as agent.approval_bridge's own qty-override guard
# (1e-9) -- floating-point notional/qty comparisons, not a real risk
# tolerance being loosened.
_DRIFT_EPSILON = 1e-6


class ExecutionError(Exception):
    pass


class DriftDetected(ExecutionError):
    """Current broker state no longer supports the exact, approved order --
    see this module's own "DRIFT CHECKS" docstring section."""


class SessionClosed(ExecutionError):
    """`execute_approved_request` refused to submit because, at the instant
    submission was actually attempted, this account's permitted trading
    session was not open -- or could not be determined at all. See this
    module's own "SESSION GATE" docstring section for the full reasoning
    (session-gate unit, 2026-08-13, closing a defect named in the
    overnight-hardening unit's own final report: this CLI path had no
    session enforcement of its own before this unit). No override exists
    today; a future capability that explicitly permits after-hours
    submission would need its own, independently-gated, default-deny
    check -- this exception does not become bypassable by accident."""


class ModeNotPermitted(ExecutionError):
    """`execute_approved_request` refused to submit because the persisted
    `agent.mode_store.ModeStore` state, read fresh at the instant of
    submission, is not one of `_SUBMISSION_PERMITTED_MODES`. See module
    docstring's "MODE + RECONCILIATION GATE" section (security-remediation
    unit, 2026-08-15) -- covers PAUSED, DISABLED, RESEARCH, an unreadable
    or never-written ModeStore, and any unrecognized mode value. No
    override exists; this is not bypassable by a caller-supplied flag."""


class ReconciliationNotFresh(ExecutionError):
    """`execute_approved_request` refused to submit because `agent.
    runtime_status`, read fresh at the instant of submission, is missing,
    not `reconciliation_status == "PASS"`, or stale per `agent.
    runtime_status.is_stale`. See module docstring's "MODE + RECONCILIATION
    GATE" section (security-remediation unit, 2026-08-15). No override
    exists; an operator who believes reconciliation is actually healthy
    must run `--reconcile-once` (or wait for the next scheduled cycle) to
    produce a fresh, PASSing snapshot -- never bypass this check."""


class QuoteUnavailable(ExecutionError):
    """`execute_approved_request` refused to submit because `quote_
    provider(staged.symbol)` returned `None`, a non-positive value, or
    raised -- see module docstring's "PRICE BAND" section
    (security-remediation unit, 2026-08-15). No fallback to a
    caller-supplied or stale price exists; this is deliberate -- the whole
    point of this exception is that a plain operator-typed number is no
    longer an acceptable substitute for a real, fresh quote."""


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


# Default-deny allowlist, not a PAUSED/DISABLED blocklist -- see module
# docstring's "MODE + RECONCILIATION GATE" section for why this shape
# matters (an unrecognized future mode value fails closed automatically).
_SUBMISSION_PERMITTED_MODES = frozenset({"PAPER", "PRODUCTION_ACTIVE"})


def _mode_permits_submission(mode_store_path: str | Path) -> tuple[bool, str]:
    """Reads `agent.mode_store.ModeStore` FRESH from `mode_store_path` --
    never a caller-held object, see module docstring -- and returns
    `(True, "")` only if the persisted mode is in `_SUBMISSION_PERMITTED_
    MODES`. Any exception while constructing/reading the store (missing
    parent directory aside -- `ModeStore` tolerates a not-yet-existing
    file, returning `current() is None`, normalized to "DISABLED" -- but a
    genuinely corrupt or unreadable file raises) is treated as "cannot
    determine the mode" and refuses, fail-closed, exactly like
    `_session_permits_submission` already does for calendar/session
    uncertainty below."""
    try:
        store = ModeStore(mode_store_path)
        persisted = mode_module.normalize_persisted(store.current())
    except Exception as exc:   # noqa: BLE001 -- fail closed on ANY read failure
        return False, f"could not read the persisted operational mode ({exc})"
    if persisted not in _SUBMISSION_PERMITTED_MODES:
        return False, (
            f"persisted mode is {persisted!r}, not one of "
            f"{sorted(_SUBMISSION_PERMITTED_MODES)}"
        )
    return True, ""


def _reconciliation_is_fresh(runtime_status_path: str | Path, *,
                             now: datetime) -> tuple[bool, str]:
    """Reads `agent.runtime_status` FRESH from `runtime_status_path` at
    `now` -- see module docstring's "MODE + RECONCILIATION GATE" section.
    Refuses on: an unreadable/corrupt file (any exception), a file that
    has never been written (`read()` returns `None`), a recorded
    `reconciliation_status` that is not exactly `"PASS"`, or a PASSing
    snapshot that is nonetheless stale per `runtime_status_module.
    is_stale` (the SAME shared staleness definition the dashboard already
    uses -- not a new threshold invented here)."""
    try:
        status = runtime_status_module.read(runtime_status_path)
    except Exception as exc:   # noqa: BLE001 -- fail closed on ANY read failure
        return False, f"could not read runtime_status.json ({exc})"
    if status is None:
        return False, (
            "no runtime_status.json has ever been written -- reconciliation "
            "health cannot be confirmed"
        )
    if status.reconciliation_status != "PASS":
        return False, (
            f"last recorded reconciliation_status is "
            f"{status.reconciliation_status!r}, not PASS"
        )
    if runtime_status_module.is_stale(status, now=now):
        return False, (
            f"runtime_status.json is stale (generated_at="
            f"{status.generated_at.isoformat()}, now={now.isoformat()})"
        )
    return True, ""


def _session_permits_submission(now: datetime) -> bool:
    """True iff `now` falls within the SAME authoritative session
    definition `agent.run_loop.in_session_now` already uses for the
    scheduled reconciliation loop -- the only real "regular session"
    notion this codebase's calendar defines (`agent.market_calendar`).
    Reused, not reimplemented -- see module docstring's "SESSION GATE"
    section.

    ANY exception from `in_session_now` (an out-of-range calendar date --
    `agent.market_calendar.CalendarCoverageError` -- a naive datetime, or
    anything else) is treated as "cannot determine the session" and
    returns `False` -- fail CLOSED, per this codebase's own fail-safe-to-
    NO-TRADE invariant (session state is exactly the kind of policy/
    process-health uncertainty that invariant already covers)."""
    try:
        return in_session_now(now)
    except Exception:
        return False


def execute_approved_request(
    request_id: str, *, store: ApprovalRequestStore, adapter: BrokerAdapter,
    gatekeeper: Gatekeeper, token: ApprovalToken,
    mode_store_path: str | Path, runtime_status_path: str | Path,
    quote_provider: QuoteProvider,
) -> BrokerOrder:
    """Verify the persisted `StagedOrder` against current broker state and
    submit it. No `now` parameter: `BrokerAdapter.submit` always derives
    "now" from `adapter.clock()` itself (never a caller-supplied value --
    see that method's own body), so a `now` accepted and threaded through
    here would be silently ignored at the one place it would matter,
    misleadingly implying a control this function does not actually have.

    `mode_store_path`/`runtime_status_path` (security-remediation unit,
    2026-08-15, REQUIRED -- no default, so no caller can silently omit
    them): see module docstring's "MODE + RECONCILIATION GATE" section.
    Both are read FRESH from disk immediately before the one real
    `adapter.submit(...)` call this function makes, regardless of what
    (if anything) the caller checked beforehand.

    `quote_provider` (security-remediation unit, 2026-08-15, REQUIRED --
    REPLACES the old `reference_price: float` parameter entirely): see
    module docstring's "PRICE BAND" section. Called for `staged.symbol`
    immediately before token consumption/submission; its return value,
    not anything the caller separately believes the price to be, is what
    is passed to `adapter.submit(..., reference_price=...)`.

    Raises `ExecutionError` (or a subclass) on any refusal this module
    itself detects -- including the new `ModeNotPermitted`/
    `ReconciliationNotFresh` -- and propagates whatever `agent.
    approval_trigger.staged_order_from_snapshot`/`agent.broker.base.
    BrokerAdapter.submit` themselves raise unchanged (`MissingStagedOrder`,
    `MissingApproval`, `OrderMismatch`, `PriceOutOfBand`, `TokenExpired`,
    `TokenConsumed`, `CrossAccountError`, `StagingForged`, a capability
    `Rejected`, ...) -- see module docstring for why none of those are
    re-implemented or swallowed here."""
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

    # Durable consumption (module docstring's "TOKEN CONSUMPTION IS NOW
    # DURABLE" section). `submit()` calls this sink immediately after its
    # own in-memory `consume()` succeeds and before the broker is ever
    # contacted -- the closure captures `request_id` and `store`, and reads
    # `tok.consumed_at` (set by that same `consume()` call, always
    # non-None by the time the sink runs) rather than `now`, so the
    # durably-recorded instant is the token's own, not this function's.
    adapter.attach_token_consumption_sink(
        lambda tok: store.record_token_consumed(
            request_id, token_snapshot=encode_token(tok), now=tok.consumed_at,
        )
    )

    # `now` is read ONCE, here, from `adapter.clock()` -- the SAME instant
    # every gate below (mode, reconciliation, session) is checked against,
    # exactly the reasoning module docstring's own "SESSION GATE" section
    # already established for session state alone; this unit extends the
    # same "read once, right before the one real submit call" posture to
    # two more kinds of uncertainty.
    now = adapter.clock()

    # MODE + RECONCILIATION GATE (module docstring's own section,
    # security-remediation unit, 2026-08-15; SAFETY-CRITICAL finding,
    # treated as a production blocker). Checked BEFORE the session gate
    # (order among these three does not matter for correctness -- all
    # three must pass, all three read fresh, none of them re-runs a
    # gate `Gatekeeper.stage` already ran) but named first here because
    # it is the one this unit exists to add.
    mode_ok, mode_reason = _mode_permits_submission(mode_store_path)
    if not mode_ok:
        raise ModeNotPermitted(
            f"request {request_id}: refusing to submit -- {mode_reason}. "
            f"Submission is permitted only while the persisted mode is one "
            f"of {sorted(_SUBMISSION_PERMITTED_MODES)}; PAUSED, DISABLED, "
            "RESEARCH, an unreadable ModeStore, and any unrecognized mode "
            "value all refuse, fail-closed, by design. No override exists."
        )

    fresh_ok, fresh_reason = _reconciliation_is_fresh(runtime_status_path, now=now)
    if not fresh_ok:
        raise ReconciliationNotFresh(
            f"request {request_id}: refusing to submit -- {fresh_reason}. "
            "A fresh, PASSing reconciliation snapshot is required "
            "immediately before submission; fail-closed on any "
            "uncertainty. No override exists."
        )

    # SESSION GATE (module docstring's own "SESSION GATE" section). The
    # LAST check before the one call in this function that can place a
    # real order -- reuses the SAME `now` read above, so a request that
    # sat DECIDED for a while is checked against the CURRENT instant, not
    # whatever instant it was approved or staged at. No override; fails
    # closed on "cannot be determined" exactly like "closed".
    if not _session_permits_submission(now):
        raise SessionClosed(
            f"request {request_id}: refusing to submit at {now.isoformat()} "
            "-- outside a permitted trading session, or the session could "
            "not be determined. No override exists; see this module's own "
            "SESSION GATE docstring section."
        )

    # PRICE BAND: A FRESH, INDEPENDENTLY-OBTAINED QUOTE, NOT A
    # CALLER-SUPPLIED NUMBER (module docstring's own "PRICE BAND" section,
    # security-remediation unit, 2026-08-15; MEDIUM finding, Codex
    # Security scan). Called last, immediately before the one real
    # `adapter.submit(...)` call -- any exception, `None`, or a
    # non-positive value is treated identically: refuse, fail closed, no
    # fallback to a stale or operator-guessed price.
    try:
        fresh_price = quote_provider(staged.symbol)
    except Exception as exc:   # noqa: BLE001 -- fail closed on ANY provider failure
        raise QuoteUnavailable(
            f"request {request_id}: refusing to submit -- quote_provider "
            f"raised obtaining a fresh price for {staged.symbol!r} ({exc}). "
            "No override exists; a caller-supplied price is never accepted "
            "as a substitute."
        ) from exc
    if fresh_price is None or fresh_price <= 0:
        raise QuoteUnavailable(
            f"request {request_id}: refusing to submit -- quote_provider "
            f"returned no usable price for {staged.symbol!r} "
            f"(got {fresh_price!r}). No override exists; a caller-supplied "
            "price is never accepted as a substitute."
        )

    return adapter.submit(staged, approval_token=token, reference_price=fresh_price)
