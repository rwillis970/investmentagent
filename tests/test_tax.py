"""Tax-lot classification (multi-account addendum).

The property this module exists to guarantee: a retirement account's lot
NEVER runs the taxable-account arithmetic, even on an input profile
constructed specifically to trip the wash-sale flag. That branch is checked
first in agent.tax.classify and returns unconditionally -- proven here by
feeding an IRA a loss-plus-repurchase profile that WOULD flag a wash sale
under the taxable branch, and asserting it comes back NOT_APPLICABLE anyway.
"""
from datetime import datetime, timedelta, timezone

import pytest

from agent.accounts import AccountType
from agent.tax import TaxCharacter, classify

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


@pytest.mark.parametrize("t", [AccountType.ROTH_IRA, AccountType.TRADITIONAL_IRA])
def test_retirement_accounts_are_never_classified_even_with_a_wash_sale_shape(t):
    result = classify(account_type=t, opened_at=T0, closed_at=T0 + timedelta(days=5),
                      proceeds=90.0, cost_basis=100.0,   # a loss...
                      repurchased_within_window=True)     # ...and repurchased
    assert result.character is TaxCharacter.NOT_APPLICABLE
    assert result.wash_sale_flag is False
    assert result.realized_gain is None


def test_taxable_short_term_under_365_days():
    r = classify(account_type=AccountType.TAXABLE, opened_at=T0,
                closed_at=T0 + timedelta(days=364), proceeds=110.0, cost_basis=100.0)
    assert r.character is TaxCharacter.SHORT_TERM
    assert r.realized_gain == 10.0


def test_taxable_long_term_over_365_days():
    r = classify(account_type=AccountType.TAXABLE, opened_at=T0,
                closed_at=T0 + timedelta(days=366), proceeds=110.0, cost_basis=100.0)
    assert r.character is TaxCharacter.LONG_TERM


def test_365_days_exactly_is_still_short_term():
    """The boundary is > 365 days, not >=: held for exactly the threshold is
    still short-term."""
    r = classify(account_type=AccountType.TAXABLE, opened_at=T0,
                closed_at=T0 + timedelta(days=365), proceeds=110.0, cost_basis=100.0)
    assert r.character is TaxCharacter.SHORT_TERM


def test_wash_sale_requires_both_a_loss_and_a_repurchase():
    loss_no_repurchase = classify(account_type=AccountType.TAXABLE, opened_at=T0,
                                  closed_at=T0 + timedelta(days=5),
                                  proceeds=90.0, cost_basis=100.0,
                                  repurchased_within_window=False)
    assert loss_no_repurchase.wash_sale_flag is False

    gain_with_repurchase = classify(account_type=AccountType.TAXABLE, opened_at=T0,
                                    closed_at=T0 + timedelta(days=5),
                                    proceeds=110.0, cost_basis=100.0,
                                    repurchased_within_window=True)
    assert gain_with_repurchase.wash_sale_flag is False

    loss_with_repurchase = classify(account_type=AccountType.TAXABLE, opened_at=T0,
                                    closed_at=T0 + timedelta(days=5),
                                    proceeds=90.0, cost_basis=100.0,
                                    repurchased_within_window=True)
    assert loss_with_repurchase.wash_sale_flag is True


def test_breakeven_is_not_a_loss():
    r = classify(account_type=AccountType.TAXABLE, opened_at=T0,
                closed_at=T0 + timedelta(days=5), proceeds=100.0, cost_basis=100.0,
                repurchased_within_window=True)
    assert r.realized_gain == 0.0
    assert r.wash_sale_flag is False
