"""Tax-lot classification (multi-account addendum to v1.1).

The plan's §4.5 mentions wash-sale prevention via cooldown as a policy
guarantee but does not specify a tax-classification module, because it did
not anticipate more than one account type. Retirement accounts have no
realized gain or loss and no wash-sale rule at all -- `classify()` makes
that a structural branch rather than a config flag or a comment: it returns
NOT_APPLICABLE / None immediately for ROTH_IRA and TRADITIONAL_IRA (via
`AccountType.is_retirement`), and computes short/long-term and wash-sale
normally for TAXABLE.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum

from .accounts import AccountType

LONG_TERM_THRESHOLD = timedelta(days=365)
# 61-day window: 30 days on each side of the sale, inclusive of the sale day.
WASH_SALE_WINDOW = timedelta(days=30)


class TaxCharacter(Enum):
    SHORT_TERM = "short_term"
    LONG_TERM = "long_term"
    NOT_APPLICABLE = "not_applicable"


@dataclass(frozen=True)
class TaxClassification:
    character: TaxCharacter | None
    wash_sale_flag: bool
    realized_gain: float | None


def classify(*, account_type: AccountType, opened_at: datetime, closed_at: datetime,
             proceeds: float, cost_basis: float,
             repurchased_within_window: bool = False) -> TaxClassification:
    """Classify a closed lot's tax treatment.

    Retirement accounts return NOT_APPLICABLE immediately -- no realized
    gain/loss, no wash-sale rule, regardless of holding period or whether a
    repurchase occurred. This branch is checked first and returns before any
    of the taxable-account arithmetic runs, so the retirement path can never
    quietly reuse the taxable math (proven by a test that builds a loss-plus-
    repurchase profile that WOULD flag a wash sale under the taxable branch).

    Taxable accounts get short/long-term classification from the holding
    period (>365 days is long-term) and a wash-sale flag when the lot is a
    loss AND was repurchased within the 61-day window -- a gain repurchased
    the same day is not a wash sale, it's just a trade.
    """
    if account_type.is_retirement:
        return TaxClassification(character=TaxCharacter.NOT_APPLICABLE,
                                 wash_sale_flag=False, realized_gain=None)

    realized_gain = proceeds - cost_basis
    held = closed_at - opened_at
    character = (TaxCharacter.LONG_TERM if held > LONG_TERM_THRESHOLD
                else TaxCharacter.SHORT_TERM)
    wash_sale_flag = realized_gain < 0 and repurchased_within_window

    return TaxClassification(character=character, wash_sale_flag=wash_sale_flag,
                             realized_gain=realized_gain)
