"""Account as a first-class dimension (multi-account addendum).

The one invariant this module exists to make impossible to violate by
accident: no code path combines two accounts' money, capacity or capability
into one number that could be fed back into risk_constrain or
Gatekeeper.stage. aggregate_report is the one sanctioned exception, and it is
checked here for exactly the property that makes it safe -- it returns a
plain dict, never a risk.PortfolioState.
"""
import pytest

from agent.accounts import (AccountReport, AccountType, BrokerCredentials,
                            CrossAccountError, aggregate_report)
from agent.risk import PortfolioState


def test_taxable_is_not_retirement():
    assert AccountType.TAXABLE.is_retirement is False


@pytest.mark.parametrize("t", [AccountType.ROTH_IRA, AccountType.TRADITIONAL_IRA])
def test_iras_are_retirement(t):
    assert t.is_retirement is True


def test_cross_account_error_names_expected_got_and_where():
    exc = CrossAccountError("acct-a", "acct-b", "DayTradeGuard.reconcile")
    assert exc.expected == "acct-a"
    assert exc.got == "acct-b"
    assert exc.where == "DayTradeGuard.reconcile"
    assert "acct-a" in str(exc) and "acct-b" in str(exc)
    assert "refusing to net or reconcile across accounts" in str(exc)


def test_broker_credentials_are_frozen():
    c = BrokerCredentials(account_id="acct-a", key_id="k1", secret_ref="keychain:k1")
    with pytest.raises(Exception):
        c.account_id = "acct-b"


def test_aggregate_report_sums_and_breaks_down_by_account():
    reports = [
        AccountReport(account_id="acct-taxable", account_type=AccountType.TAXABLE,
                     nlv=1000.0, settled_cash=800.0),
        AccountReport(account_id="acct-ira", account_type=AccountType.ROTH_IRA,
                     nlv=500.0, settled_cash=500.0),
    ]
    out = aggregate_report(reports)
    assert out["accounts"] == ("acct-taxable", "acct-ira")
    assert out["total_nlv"] == 1500.0
    assert out["total_settled_cash"] == 1300.0
    assert out["by_account"]["acct-taxable"] == {
        "account_type": "TAXABLE", "nlv": 1000.0, "settled_cash": 800.0,
    }
    assert out["by_account"]["acct-ira"] == {
        "account_type": "ROTH_IRA", "nlv": 500.0, "settled_cash": 500.0,
    }


def test_aggregate_report_is_never_a_portfolio_state():
    """The structural guarantee the addendum relies on: aggregate_report's
    output cannot be mistaken for -- or accidentally passed as -- a
    single-account PortfolioState."""
    reports = [AccountReport(account_id="acct-a", account_type=AccountType.TAXABLE,
                             nlv=1000.0, settled_cash=1000.0)]
    out = aggregate_report(reports)
    assert isinstance(out, dict)
    assert not isinstance(out, PortfolioState)
    assert not hasattr(out, "account_id")


def test_aggregate_report_of_no_accounts_is_zero_not_an_error():
    out = aggregate_report([])
    assert out == {"accounts": (), "total_nlv": 0, "total_settled_cash": 0,
                   "by_account": {}}
