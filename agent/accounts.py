"""Account as a first-class dimension (multi-account addendum to v1.1 -- see
docs/multi-account-addendum.md).

Before this module, the system assumed exactly one account. It now assumes
there is always a SPECIFIC account -- every risk, reserve, holding, day-trade
and tax computation takes an `account_id` and refuses to guess when one
doesn't match another. There is no code path that combines two accounts'
money, capacity or capability into one number. The one exception is
`aggregate_report` at the bottom of this file, which is read-only, returns a
plain `dict`, and is structurally incapable of being fed into
`risk_constrain` or `Gatekeeper.stage` -- both require a single-account
`PortfolioState`. That is the aggregate-reporting-vs-aggregate-risk line,
enforced by the type system rather than left as a comment.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class AccountType(str, Enum):
    TAXABLE = "TAXABLE"
    ROTH_IRA = "ROTH_IRA"
    TRADITIONAL_IRA = "TRADITIONAL_IRA"

    @property
    def is_retirement(self) -> bool:
        """Roth and traditional IRAs are tax-advantaged: no realized gain or
        loss, no wash-sale rule. Taxable is the only account type where those
        concepts apply at all."""
        return self is not AccountType.TAXABLE


class CrossAccountError(Exception):
    """Raised whenever an operation's account_id does not match the
    account_id of the object it is being applied to -- a PortfolioState
    handed to the wrong Gatekeeper, a StagedOrder submitted to the wrong
    adapter, a reconciliation snapshot from the wrong account. This is a hard
    stop, never a merge, per the no-cross-account-netting invariant."""

    def __init__(self, expected: str, got: str, where: str):
        self.expected, self.got, self.where = expected, got, where
        super().__init__(
            f"{where}: expected account {expected!r}, got {got!r} -- "
            "refusing to net or reconcile across accounts"
        )


@dataclass(frozen=True)
class BrokerCredentials:
    """A REFERENCE to a credential, never the credential itself -- the same
    principle as §8.1's OS-keychain entries. Exactly one of these belongs to
    exactly one account_id. An adapter constructed with account A's
    credentials must never be handed account B's orders; `BrokerAdapter`
    enforces this by refusing to submit a StagedOrder whose account_id
    doesn't match its own (see broker/base.py)."""
    account_id: str
    key_id: str
    secret_ref: str        # e.g. a keychain entry name -- never a raw secret


@dataclass(frozen=True)
class AccountReport:
    """A read-only snapshot for cross-account REPORTING only. Never an input
    to risk_constrain, Gatekeeper.stage, or any order path -- those all
    require a `risk.PortfolioState`, a distinct type this is never converted
    to or accepted in place of."""
    account_id: str
    account_type: AccountType
    nlv: float
    settled_cash: float


def aggregate_report(reports: list[AccountReport]) -> dict:
    """The one sanctioned way to combine numbers across accounts: a total for
    a dashboard. Returns a plain dict -- deliberately NOT a PortfolioState --
    so it cannot be passed to risk_constrain or Gatekeeper.stage by mistake;
    both require a single-account PortfolioState and would fail a type/attr
    check immediately if handed this instead."""
    return {
        "accounts": tuple(r.account_id for r in reports),
        "total_nlv": sum(r.nlv for r in reports),
        "total_settled_cash": sum(r.settled_cash for r in reports),
        "by_account": {r.account_id: {"account_type": r.account_type.value,
                                      "nlv": r.nlv,
                                      "settled_cash": r.settled_cash}
                       for r in reports},
    }
