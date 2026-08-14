"""One-time historical repair for the cash-seed-ordering incident found
against the real paper account 2026-08-14 (see `agent.account_wiring`'s own
module docstring, "REAL INCIDENT THIS FIX CLOSES", for the full story, and
`tests/test_ledger_store.py::test_opening_balance_correction_is_the_exact_
shape_of_the_real_task2_repair` for the same arithmetic proven against a
disposable store).

WHAT WENT WRONG, IN ONE LINE. `write_opening_balance` was called at a moment
the SPY BUY that funded the account's very first trade had been quarantined
(not yet a durable `Fill`), so it seeded the broker's raw, ALREADY-POST-FILL
cash figure verbatim; the fill (and a real CAT-fee `CashAdjustment`, already
reflected in that same broker figure) were then admitted and replayed
through the ordinary unconditional BUY-debit / cash-adjustment paths in
`Ledger.settled_cash()`, double-counting both.

WHAT THIS SCRIPT DOES, AND DOES NOT. Read-only by default (`--dry-run`,
which is also the default with no flag at all): loads the REAL
`data/ledger.jsonl` through the real `LedgerStore`/`Ledger` classes,
unmodified, computes the exact `OpeningBalanceCorrection` this incident
calls for using the SAME arithmetic `LedgerStore.seed_opening_balance_from_
broker` already uses elsewhere in this codebase (broker figure minus the
combined effect of everything that was already reflected in it), and prints
every number and the exact row -- never writes it. Writing it requires the
separate, explicit `--apply` flag (see bottom of this docstring) which THIS
SCRIPT SUPPORTS but which no automated run of this script should invoke
without an operator reading the dry-run output first.

THE RULE FOR "ALREADY REFLECTED IN THE BROKER FIGURE AT SEED TIME": a Fill
or CashAdjustment counts if its own economic timestamp (`filled_at` /
`effective_date`) is at or before the `opening_balance` row's own `at` --
broker cash reflects a BUY's debit and a fee's debit immediately, at the
trade's own timestamp, not at whatever later moment this ledger happened to
record them (see `agent/ledger.py`'s own CASH ADJUSTMENTS section for the
fee case and DECISION 3 for the fill case). This is exactly the criterion
`seed_opening_balance_from_broker` implicitly uses when it is called at the
correct moment (after the fill/adjustment are already durably recorded,
before any further trading) -- this script recovers the number that path
would have produced, for the one historical seed that used the wrong path.

NO BROKER CALL. This script never constructs a `BrokerAdapter` and never
touches the network or credentials -- the broker's $480.00 settled-cash
figure this repair targets is the one already established as fact in the
Phase 1 investigation (real `/api/state`/adapter read, reported to the
operator separately), not re-fetched here. Comparing the in-memory result
against that already-known figure is a printed assertion, not a live call.

IDEMPOTENCY / SAFETY. If an `OpeningBalanceCorrection` already exists for
this `opening_balance` row's own `at`, this script refuses to propose a
second one (append-only discipline: one correction per corrected seed,
matching `write_opening_balance`'s own "exactly once" contract for the row
it corrects). If more than one `opening_balance` row exists (should never
happen -- the store itself refuses a second one), or none exist, or the
proposed correction amount is exactly zero, this script reports that and
proposes nothing.

Usage:
    python3 scripts/repair_settled_cash_baseline.py \
        --ledger-path data/ledger.jsonl --account-id PA3XZX944LRR

    (dry-run is the default; add --dry-run explicitly if you want to be
    unambiguous in a script. No flag combination in dry-run mode writes
    anything.)

THE SEPARATE MUTATION COMMAND (not run by this script, not run in the
session that wrote this script -- an operator decision, made after reading
the dry-run output below):

    python3 scripts/repair_settled_cash_baseline.py \
        --ledger-path data/ledger.jsonl --account-id PA3XZX944LRR \
        --apply --confirmed

`--apply` alone is refused -- both flags are required together (same
"explicit, not implied" discipline `scripts/run_agent.py --confirmed`
already uses for other irreversible actions), and `--apply` performs
exactly one `store.write_opening_balance_correction(...)` call with the
precise row printed by the dry-run, nothing else.
"""
from __future__ import annotations

import argparse
import json
import sys
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.holding import HoldingPolicy, HoldingPolicyRegistry
from agent.ledger import Ledger, OpeningBalanceCorrection
from agent.ledger_store import LedgerStore, LedgerStoreError
from agent.money import to_decimal


def _print_header(title: str) -> None:
    print()
    print("=" * len(title))
    print(title)
    print("=" * len(title))


def _permissive_registry(fill_rows) -> HoldingPolicyRegistry:
    """Every BUY fill's own `holding_policy_version` must resolve against
    something, or `LedgerStore.load()` itself raises before this script
    gets a chance to print anything -- this script is read-only and has no
    business asserting real minimum-hold/cooldown durations (that's
    `agent/config.py`'s job, not this diagnostic's), so it registers each
    version actually present in the real file with a zero-duration policy.
    Safe here specifically because this script never calls `sellable_qty`/
    `check_normal_exit` -- only `settled_cash()`/`positions()`, neither of
    which consult the policy's duration fields at all."""
    versions = {r.get("holding_policy_version") for r in fill_rows
               if r.get("side", "").upper() == "BUY" and r.get("holding_policy_version")}
    return HoldingPolicyRegistry([
        HoldingPolicy(version=v, minimum_holding_period=timedelta(0), cooldown_period=timedelta(0))
        for v in versions
    ])


def _read_raw_rows(ledger_path: Path) -> list[dict]:
    if not ledger_path.exists():
        return []
    rows = []
    with ledger_path.open() as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--ledger-path", default="data/ledger.jsonl",
                        help="path to the real canonical ledger file (default: data/ledger.jsonl)")
    parser.add_argument("--account-id", required=True,
                        help="the account_id this ledger is bound to (e.g. PA3XZX944LRR)")
    parser.add_argument("--now", default=None,
                        help="ISO-8601 timezone-aware instant to derive settled_cash as-of "
                             "(default: current UTC time)")
    parser.add_argument("--dry-run", action="store_true", default=True,
                        help="(default) print the proposed repair, write nothing")
    parser.add_argument("--apply", action="store_true", default=False,
                        help="perform the real, single append-only write -- requires --confirmed too")
    parser.add_argument("--confirmed", action="store_true", default=False,
                        help="required alongside --apply; on its own, does nothing")
    args = parser.parse_args(argv)

    if args.apply and not args.confirmed:
        print("REFUSING: --apply requires --confirmed as well (both flags, "
              "not implied by either alone). Nothing was written.", file=sys.stderr)
        return 2
    if args.confirmed and not args.apply:
        print("REFUSING: --confirmed on its own does nothing -- pass --apply too, "
              "or drop --confirmed to stay in dry-run mode.", file=sys.stderr)
        return 2

    now = (datetime.fromisoformat(args.now) if args.now
          else datetime.now(timezone.utc))
    if now.tzinfo is None:
        print("REFUSING: --now must be timezone-aware.", file=sys.stderr)
        return 2

    ledger_path = Path(args.ledger_path)
    raw_rows = _read_raw_rows(ledger_path)

    _print_header("STEP 1 -- CURRENT ROWS (read-only, exactly as persisted)")
    if not raw_rows:
        print(f"(no rows found at {ledger_path})")
    for row in raw_rows:
        print(json.dumps(row, indent=None, sort_keys=True))

    fill_rows = [r for r in raw_rows if r.get("kind") == "fill"]
    registry = _permissive_registry(fill_rows)
    store = LedgerStore(ledger_path, account_id=args.account_id, policy_registry=registry)

    opening_amount, fills, order_records = store.load()
    if opening_amount is None:
        print("\nNo opening_balance row exists yet -- nothing for this script to repair. "
              "(This is the ordinary fresh-install state, not the incident this script "
              "targets.)")
        return 0
    opening_at = store.opening_balance_established_at()

    ledger = store.to_ledger()

    _print_header("STEP 2 -- CURRENT DERIVED SETTLED CASH")
    current_settled_cash = ledger.settled_cash(now=now)
    print(f"opening_settled_cash (raw, as seeded) = {opening_amount}")
    print(f"opening_balance.at                     = {opening_at.isoformat()}")
    print(f"local fills recorded                   = {len(ledger.fills)}")
    print(f"local cash_adjustments recorded         = {len(ledger.cash_adjustments)}")
    print(f"existing opening_balance_corrections    = {len(ledger.opening_balance_corrections)}")
    print(f"Ledger.settled_cash(now={now.isoformat()}) = {current_settled_cash}")

    for existing in ledger.opening_balance_corrections:
        if existing.corrects_opening_balance_established_at == opening_at:
            _print_header("ALREADY REPAIRED")
            print(f"An OpeningBalanceCorrection already exists for this opening_balance "
                 f"row (correction_id={existing.correction_id!r}, amount={existing.amount}, "
                 f"reason={existing.reason!r}). Refusing to propose a second one. "
                 "Nothing to do.")
            return 0

    _print_header("STEP 3 -- WHAT WAS ALREADY REFLECTED IN THE BROKER FIGURE AT SEED TIME")
    pre_seed_fills = [f for f in ledger.fills if f.filled_at <= opening_at]
    pre_seed_adjustments = [a for a in ledger.cash_adjustments
                            for r in raw_rows
                            if r.get("kind") == "cash_adjustment"
                            and r["adjustment_id"] == a.adjustment_id
                            and datetime.fromisoformat(r["effective_date"] + "T00:00:00+00:00")
                                <= opening_at]
    if not pre_seed_fills and not pre_seed_adjustments:
        print("No fill or cash adjustment has a timestamp at or before the "
             f"opening_balance's own at ({opening_at.isoformat()}) -- this "
             "specific incident's signature is absent. Proposing no correction.")
        return 0

    for f in pre_seed_fills:
        notional = to_decimal(f.qty) * to_decimal(f.price)
        print(f"  fill {f.fill_id}: {f.side} {f.qty} {f.symbol} @ {f.price} "
             f"filled_at={f.filled_at.isoformat()} (<= seed at) -- raw notional {notional}")
    for a in pre_seed_adjustments:
        print(f"  cash_adjustment {a.adjustment_id}: {a.amount} "
             f"({a.activity_type}, {a.description!r})")

    # Same equation `LedgerStore.seed_opening_balance_from_broker` already
    # uses elsewhere: replay ONLY the pre-seed fills/adjustments against a
    # placeholder opening of 0 -- their combined effect is exactly what
    # should have been backed out of the broker's raw figure at seed time,
    # and wasn't.
    placeholder = Ledger(account_id=args.account_id, opening_settled_cash=Decimal("0"),
                         policy_registry=registry)
    for f in pre_seed_fills:
        placeholder.record_fill(f)
    for a in pre_seed_adjustments:
        placeholder.record_cash_adjustment(a)
    pre_seed_combined_effect = placeholder.settled_cash(now=opening_at)
    correction_amount = -pre_seed_combined_effect

    _print_header("STEP 4 -- PROPOSED CORRECTION")
    print(f"combined pre-seed effect (fills' BUY-debits + adjustments) = {pre_seed_combined_effect}")
    print(f"correction_amount (the amount to add back)                = {correction_amount}")

    if correction_amount == 0:
        print("\nCorrection amount is exactly zero -- nothing to repair.")
        return 0

    fill_ids = ", ".join(f.fill_id for f in pre_seed_fills)
    adjustment_ids = ", ".join(a.adjustment_id for a in pre_seed_adjustments)
    reason = (
        f"opening_balance seeded {opening_at.isoformat()} already reflected "
        f"the following, later admitted/replayed a second time through "
        f"Ledger.settled_cash()'s own unconditional fill/adjustment loops, "
        f"double-counting them: "
        + (f"fill(s) [{fill_ids}]" if fill_ids else "")
        + (" and " if fill_ids and adjustment_ids else "")
        + (f"cash_adjustment(s) [{adjustment_ids}]" if adjustment_ids else "")
        + f". Root cause: agent.account_wiring.build_account_reconciliation seeded "
          f"opening_settled_cash via write_opening_balance while the fill above was "
          f"still quarantined (0 local fills at seed time), instead of deferring "
          f"until the quarantine was resolved -- see that module's own "
          f"'REAL INCIDENT THIS FIX CLOSES' docstring section, fixed going forward "
          f"2026-08-14."
    )
    correction_id = (
        f"repair-{opening_at.strftime('%Y%m%d%H%M%S%f')}-"
        f"{uuid.uuid5(uuid.NAMESPACE_URL, opening_at.isoformat() + str(correction_amount)).hex[:8]}"
    )
    correction = OpeningBalanceCorrection(
        correction_id=correction_id, account_id=args.account_id,
        amount=correction_amount, reason=reason,
        corrects_opening_balance_established_at=opening_at,
        recorded_at=now,
    )
    row = {
        "kind": "opening_balance_correction",
        "correction_id": correction.correction_id,
        "account_id": correction.account_id,
        "amount": str(correction.amount),
        "reason": correction.reason,
        "corrects_opening_balance_established_at":
            correction.corrects_opening_balance_established_at.isoformat(),
        "recorded_at": correction.recorded_at.isoformat(),
    }
    print("\nExact append-only row to be appended to", ledger_path, ":")
    print(json.dumps(row, indent=2, sort_keys=True))

    _print_header("STEP 5 -- RESULT IF APPLIED (computed IN MEMORY ONLY -- nothing written yet)")
    ledger.record_opening_balance_correction(correction)
    resulting_settled_cash = ledger.settled_cash(now=now)
    print(f"settled_cash BEFORE correction (in memory) = {current_settled_cash}")
    print(f"settled_cash AFTER correction  (in memory) = {resulting_settled_cash}")
    print("expected result (broker settled cash, established in the Phase 1 "
         "investigation, NOT re-fetched here) = 480.00")
    if resulting_settled_cash == Decimal("480.00"):
        print("MATCH: in-memory result equals the known broker figure exactly.")
    else:
        print("NO MATCH -- do not apply this correction; the arithmetic above does "
             "not close the gap this script targets. Investigate before proceeding.")

    _print_header("STEP 6 -- EFFECT ON POSITIONS / FILLS / AUDIT")
    print(f"positions before = {store.to_ledger().positions()}")
    print(f"positions after  (in memory) = {ledger.positions()}")
    print("-> IDENTICAL: OpeningBalanceCorrection never touches self._fills "
         "(see its own record_opening_balance_correction, mirrors "
         "record_cash_adjustment's own 'cannot disturb lot accounting' guarantee).")
    print(f"fills before = {len(store.to_ledger().fills)}, "
         f"fills after (in memory) = {len(ledger.fills)} -- IDENTICAL, same reason.")
    print("Audit/reconciliation effect: this is a NEW, append-only row of a NEW kind "
         "('opening_balance_correction') -- no existing row (fill, opening_balance, "
         "cash_adjustment) is modified, reordered or deleted. "
         "agent.reconciliation.reconcile_settled_cash will see the corrected "
         "local_settled_cash the next time it runs after this row is applied, "
         "with no change to its own exact-equality discipline.")

    if not args.apply:
        _print_header("DRY RUN COMPLETE -- NOTHING WAS WRITTEN")
        print("Re-run with --apply --confirmed to perform the single real write above.")
        return 0

    _print_header("APPLYING (--apply --confirmed both present)")
    try:
        store.write_opening_balance_correction(correction)
    except LedgerStoreError as e:
        print(f"REFUSED by the store: {e}", file=sys.stderr)
        return 1
    print(f"Wrote 1 row to {ledger_path}. New settled_cash(now={now.isoformat()}) = "
         f"{store.to_ledger().settled_cash(now=now)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
