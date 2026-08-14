"""scripts/repair_settled_cash_baseline.py -- writer-lock coverage
(writer-lock-gap unit, round 2, 2026-08-14).

This script had ZERO test coverage before this file (verified: `git grep
repair_settled_cash_baseline tests/` returned nothing). It was previously
exercised only manually, in-session, against disposable synthetic copies --
see this script's own module docstring and the Task 4 final report. This
file does not attempt to re-prove the arithmetic (tests/test_ledger_store.py
::test_opening_balance_correction_is_the_exact_shape_of_the_real_task2_repair
already does that, exhaustively, at the `LedgerStore` level) -- it exists
specifically to prove `--apply` now serializes against the SAME canonical
process lock every other writer in this codebase uses, using a real
`fcntl.flock` (via `agent.process_lock.acquire_process_lock`), not a mock.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from agent.holding import HoldingPolicy, HoldingPolicyRegistry
from agent.ledger_store import LedgerStore
from agent.process_lock import acquire_process_lock
from agent.ledger import CashAdjustment, Fill, OpeningBalanceCorrection
from agent.money import to_decimal
from scripts.repair_settled_cash_baseline import main

ACCT = "acct-taxable"
SEEDED_AT = datetime(2026, 8, 12, 16, 6, 48, 185029, tzinfo=timezone.utc)
FILL_AT = datetime(2026, 7, 28, 14, 42, 51, 412408, tzinfo=timezone.utc)


def _registry():
    return HoldingPolicyRegistry([
        HoldingPolicy(version="hp-v1", minimum_holding_period=__import__("datetime").timedelta(0),
                      cooldown_period=__import__("datetime").timedelta(0)),
    ])


def _seed_real_incident_shape(ledger_path: Path) -> None:
    """The exact real three-row shape this script targets (see
    tests/test_ledger_store.py's own reproduction of the same incident):
    opening_balance=480 seeded AFTER a SPY BUY fill and a CAT-fee cash
    adjustment had already happened but before either was durably
    recorded -- --apply proposes a +20.00 OpeningBalanceCorrection."""
    store = LedgerStore(ledger_path, account_id=ACCT, policy_registry=_registry())
    store.write_cash_adjustment(CashAdjustment(
        adjustment_id="20260728000000000::de3745eb-7d16-4bf3-9514-234693d9f84e",
        account_id=ACCT, amount=to_decimal("-0.01"), activity_type="FEE",
        description="CAT fee for proceed of 1 trades on 2026-07-28 by PA3XZX944LRR",
        effective_date=datetime(2026, 7, 28).date(), symbol=None,
    ))
    store.write_opening_balance(Decimal("480"), at=SEEDED_AT)
    store.write_fill(Fill(
        fill_id="20260728104251412::37042727-dfba-4cac-a1d7-607636cd4346",
        account_id=ACCT, symbol="SPY", side="BUY", qty=to_decimal("0.027087234"),
        price=to_decimal("737.986"), filled_at=FILL_AT, lot_id="l1",
        holding_policy_version="hp-v1",
    ))


def test_dry_run_never_touches_the_lock_and_writes_nothing(tmp_path):
    ledger_path = tmp_path / "ledger.jsonl"
    _seed_real_incident_shape(ledger_path)
    before = ledger_path.read_bytes()

    # A competing holder of the SAME canonical directory's lock -- dry-run
    # (the default, no --apply) must succeed anyway, proving it never
    # contends for the lock at all.
    with acquire_process_lock(ledger_path.resolve().parent):
        code = main(["--ledger-path", str(ledger_path), "--account-id", ACCT,
                    "--now", "2026-08-14T15:00:00+00:00"])
    assert code == 0
    assert ledger_path.read_bytes() == before


def test_apply_refuses_and_writes_nothing_while_the_scheduled_loop_holds_the_lock(
    tmp_path, capsys,
):
    ledger_path = tmp_path / "ledger.jsonl"
    _seed_real_incident_shape(ledger_path)
    before = ledger_path.read_bytes()

    with acquire_process_lock(ledger_path.resolve().parent):   # simulates the scheduled loop
        code = main([
            "--ledger-path", str(ledger_path), "--account-id", ACCT,
            "--now", "2026-08-14T15:00:00+00:00", "--apply", "--confirmed",
        ])
    assert code == 1
    assert ledger_path.read_bytes() == before   # byte-identical -- refused before the write
    err = capsys.readouterr().err
    assert "REFUSING" in err


def test_apply_succeeds_and_writes_exactly_one_row_once_the_lock_is_free(tmp_path):
    ledger_path = tmp_path / "ledger.jsonl"
    _seed_real_incident_shape(ledger_path)
    rows_before = len(ledger_path.read_text().splitlines())

    code = main([
        "--ledger-path", str(ledger_path), "--account-id", ACCT,
        "--now", "2026-08-14T15:00:00+00:00", "--apply", "--confirmed",
    ])
    assert code == 0
    rows_after = ledger_path.read_text().splitlines()
    assert len(rows_after) == rows_before + 1

    store = LedgerStore(ledger_path, account_id=ACCT, policy_registry=_registry())
    ledger = store.to_ledger()
    assert ledger.settled_cash(now=datetime(2026, 8, 14, 15, 0, tzinfo=timezone.utc)) \
        == Decimal("480.00")
    assert len(ledger.opening_balance_corrections) == 1


def test_a_second_apply_after_the_first_refuses_as_a_duplicate_not_a_lock_error(tmp_path):
    """Sanity check that the lock and the pre-existing idempotency guard
    are two independent protections, not the same one -- a second --apply
    with NO competing lock held must still refuse (duplicate correction),
    proving the lock addition didn't paper over the idempotency check."""
    ledger_path = tmp_path / "ledger.jsonl"
    _seed_real_incident_shape(ledger_path)

    first = main(["--ledger-path", str(ledger_path), "--account-id", ACCT,
                 "--now", "2026-08-14T15:00:00+00:00", "--apply", "--confirmed"])
    assert first == 0
    rows_after_first = len(ledger_path.read_text().splitlines())

    second = main(["--ledger-path", str(ledger_path), "--account-id", ACCT,
                  "--now", "2026-08-14T15:05:00+00:00", "--apply", "--confirmed"])
    assert second == 0   # refuses cleanly, not an error exit -- see ALREADY REPAIRED branch
    assert len(ledger_path.read_text().splitlines()) == rows_after_first   # no second row
