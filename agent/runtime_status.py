"""A durable, current-state operational snapshot for operators and the
dashboard (overnight-hardening unit, 2026-08-13).

THIS IS NOT AN AUDIT LOG. `agent.audit.AuditLog` is the permanent,
append-only, hash-chained record of what happened, when -- this module is
the OPPOSITE of that on purpose: one small JSON document, overwritten every
time something worth showing changes, holding only "what does the system
believe right now." Losing this file loses nothing durable -- it can always
be rebuilt from a fresh cycle or diagnostic run. `AuditLog`/`LedgerStore`/
`ModeStore` remain the only sources of truth this codebase ever reasons
from; this file is a read-optimized VIEW for a human or a dashboard, never
a second copy of anything authoritative.

NEVER CONTAINS A SECRET. Every field here is either an account identifier
already public within this deployment (account_id), a status enum, a
timestamp, or a boolean -- never a key id, a keychain reference resolved to
its actual value, or anything from `SecretsProvider.resolve`.

THREE PRODUCERS, ONE SHAPE (extended PAUSED-reconcile-follow-up runtime-
status unit, 2026-08-14). `RuntimeStatus.source` distinguishes which:

  * `"cycle"` -- written by `scripts/run_agent.py`'s own `on_cycle_success`
    hook after a REAL `agent.run_loop.run_cycle` completes: `sync_fills`
    actually polled the broker, `agent.startup.run_startup`'s own
    reconciliation actually ran and passed. This is the strongest possible
    evidence -- a live trading-session cycle, not a snapshot read. This is
    the ONLY producer that ever sets `last_successful_cycle_at` to a NEW
    value.
  * `"reconcile_once"` -- written by `scripts/run_agent.py`'s own
    `_run_reconcile_once` (`--reconcile-once`) after a REAL `agent.
    run_loop.sync_and_build_reconciliations` + `agent.startup.
    reconcile_accounts_or_raise` complete: this ALSO actually polls the
    broker and ALSO actually performs exact reconciliation -- genuinely
    stronger evidence than `"diagnostic"`, below, which never calls either.
    It is still NOT `"cycle"`: no pipeline is ever attached (no candidate
    generation, no materiality screen, no T4 analysis, no approval
    request), and `run_startup`'s own audit-chain-verification/approval-
    expiry-sweep/mode-transition machinery never runs. A `"reconcile_once"`
    snapshot proves reconciliation health -- broker reads succeed, and
    positions/settled-cash/open-orders/day-trades all genuinely agree -- it
    must NEVER be read as proof a real scheduled market-session cycle
    occurred. `last_successful_cycle_at` is CARRIED FORWARD unchanged from
    whatever it already was (see `_run_reconcile_once`'s own docstring) --
    this producer never sets it to a new value, and never fabricates a
    value where none existed.
  * `"diagnostic"` -- written by `agent.diagnostics.diagnose_account`
    (`scripts/diagnose_runtime.py`), which can run OUTSIDE a trading
    session (see that module's own docstring for why) but never calls
    `sync_fills`/`run_startup` itself -- it computes the SAME comparisons
    `agent.reconciliation` performs, read-only, against whatever the
    ledger and broker report AT THAT MOMENT, without writing a fill or
    advancing any mode. A `"diagnostic"`-sourced snapshot is real evidence
    that the system is NOT currently broken, but it is not the same claim
    as `"cycle"` -- it never confirms a genuinely NEW execution would be
    picked up and reconciled correctly, only that everything already on
    record still agrees.

Do not label a `"diagnostic"` OR a `"reconcile_once"` snapshot as proof of
a live trading cycle anywhere this is displayed -- that conflation is
exactly the kind of truthfulness gap this whole unit exists to close.
`source` is always ONE of these three exact strings; nothing in this
codebase writes a fourth value.

FIELDS WHOSE SEMANTICS THIS CODE CANNOT SUPPORT ARE NEVER INVENTED. A field
this run had no way to determine (e.g. `collection_last_success_at` from a
diagnostic run, which never touches the collection/screening pipeline) is
left `None` with a matching entry in `unavailable_reasons` explaining WHY,
never guessed or defaulted to something that merely looks plausible.

FRESHNESS/STALENESS is deliberately NOT a field on this dataclass -- it is
a property of "now minus generated_at" computed by whatever reads this file
(the dashboard, an operator script), not baked in at write time, since
"stale" has no fixed meaning independent of who's asking and how urgently.
`is_stale(status, *, now, max_age)` below is the one shared definition
every reader should use rather than each inventing its own threshold
inline."""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

# Every field an operator or the dashboard might reasonably ask about that
# this snapshot cannot determine gets a reason string keyed by field name in
# `unavailable_reasons`, rather than silently being left as an unexplained
# `None` -- "unavailable state must include an explicit reason."
DEFAULT_STALE_AFTER = timedelta(hours=25)   # a bit over one calendar day --
# generous enough that a normal weekend gap (no trading Sat/Sun) between
# Friday's close-of-day snapshot and Monday's does not itself read as
# "stale," while still catching a snapshot that is genuinely old.


@dataclass(frozen=True)
class RuntimeStatus:
    generated_at: datetime
    account_id: str
    mode: str | None
    process_status: str
    source: str                              # "cycle" | "diagnostic"

    market_session_state: str                # "OPEN" | "CLOSED"
    next_session_open: datetime | None

    broker_snapshot_status: str              # PASS | WARN | FAIL | UNAVAILABLE
    broker_snapshot_at: datetime | None

    reconciliation_status: str               # PASS | WARN | FAIL | UNAVAILABLE
    reconciliation_at: datetime | None
    positions_reconciled: bool | None
    cash_reconciled: bool | None
    open_orders_reconciled: bool | None

    last_successful_cycle_at: datetime | None
    last_failure_at: datetime | None
    last_failure_type: str | None
    recovered_at: datetime | None

    collection_last_success_at: datetime | None
    screen_last_success_at: datetime | None

    unavailable_reasons: dict[str, str]


def is_stale(status: RuntimeStatus, *, now: datetime,
            max_age: timedelta = DEFAULT_STALE_AFTER) -> bool:
    """The one shared definition of "stale" -- see module docstring for why
    this is a function of the READER's `now`, not a field baked into the
    snapshot itself."""
    return (now - status.generated_at) > max_age


def _encode(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def write_atomic(path: str | Path, status: RuntimeStatus) -> None:
    """Write-to-temp-then-`os.replace`, same technique as `agent.
    failure_sentinel.save`'s own atomic write (overnight-hardening unit,
    2026-08-13): the dashboard must never be able to read a half-written
    JSON document mid-write -- `os.replace` is atomic on both POSIX and
    Windows, so a reader either sees the complete old file or the complete
    new one, never a truncated mixture of both."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    d = {k: _encode(v) for k, v in asdict(status).items()}
    tmp = p.with_suffix(p.suffix + f".tmp-{os.getpid()}")
    tmp.write_text(json.dumps(d, indent=2, sort_keys=True))
    os.replace(tmp, p)


def _decode_datetime(value: Any) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def read(path: str | Path) -> RuntimeStatus | None:
    """`None` if the file does not exist yet (e.g. no cycle and no
    diagnostic has ever run) -- a safe, explicit "nothing recorded," not an
    exception a caller has to guard against."""
    p = Path(path)
    if not p.exists():
        return None
    d = json.loads(p.read_text())
    return RuntimeStatus(
        generated_at=_decode_datetime(d["generated_at"]),
        account_id=d["account_id"],
        mode=d.get("mode"),
        process_status=d["process_status"],
        source=d["source"],
        market_session_state=d["market_session_state"],
        next_session_open=_decode_datetime(d.get("next_session_open")),
        broker_snapshot_status=d["broker_snapshot_status"],
        broker_snapshot_at=_decode_datetime(d.get("broker_snapshot_at")),
        reconciliation_status=d["reconciliation_status"],
        reconciliation_at=_decode_datetime(d.get("reconciliation_at")),
        positions_reconciled=d.get("positions_reconciled"),
        cash_reconciled=d.get("cash_reconciled"),
        open_orders_reconciled=d.get("open_orders_reconciled"),
        last_successful_cycle_at=_decode_datetime(d.get("last_successful_cycle_at")),
        last_failure_at=_decode_datetime(d.get("last_failure_at")),
        last_failure_type=d.get("last_failure_type"),
        recovered_at=_decode_datetime(d.get("recovered_at")),
        collection_last_success_at=_decode_datetime(d.get("collection_last_success_at")),
        screen_last_success_at=_decode_datetime(d.get("screen_last_success_at")),
        unavailable_reasons=d.get("unavailable_reasons", {}),
    )
