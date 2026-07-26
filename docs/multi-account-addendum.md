# Multi-Account Addendum to v1.1

Status: implemented in code (`agent/accounts.py`, `agent/tax.py`, and
`account_id` threading through `agent/risk.py`, `agent/holding.py`,
`agent/daytrade.py`, `agent/entities.py`, `agent/pipeline.py`,
`agent/broker/base.py`, `agent/broker/simulator.py`,
`migrations/002_multi_account.sql`). This document records why, not just
what — v1.1 assumed exactly one account throughout, and every place that
assumption showed up needed a deliberate decision, not a mechanical
find-and-replace.

## Why this exists

v1.1 §4 (holding), §6.1 (risk), and §9.1 (entities/schema) are all written in
terms of *the* portfolio, *the* day-trade counter, *the* cash balance. That
was fine for a single-account pilot. The moment a second account exists —
even a second paper account, even before any IRA is involved — every one of
those singular nouns becomes a question: which account's cash? Whose lots?
Whose day-trade budget? An unanswered version of that question is exactly
the kind of bug this plan's invariants exist to make structurally
impossible (see v1.0 §7.2 on path-dependent, per-order checks for the same
class of problem in a different shape).

## Impact on the plan's own sections

- **§4 (holding).** A lot belongs to exactly one account. `sellable_qty` and
  `blocked_qty` now require `account_id` as a mandatory argument, not an
  optional filter — filtering by symbol alone would net two accounts'
  shares of the same symbol into one sellable quantity, which is the
  cross-account netting bug this addendum exists to prevent structurally
  (see `test_holding.py::test_two_accounts_holding_the_same_symbol_do_not_net`).
- **§6.1 (risk).** `PortfolioState` gained `account_id` as a required field
  with no default. `risk_constrain` itself did not need to change — it
  already operated on one account's target weight vector — but every
  caller now states which account's vector it is.
- **§9.1 (entities / schema).** `RunManifest`, `Lot` (`position_lot`),
  `Order`, and the day-trade counter all gained `account_id`.
  `migrations/002_multi_account.sql` is additive only: `001_init.sql` is
  untouched, and `test_entities_match_sql.py` now applies every migration
  in order (CREATE TABLE, then each ALTER TABLE ADD COLUMN) before
  comparing a Python entity's fields to the resulting column set, so a
  later migration's additions are checked the same way a real migration
  runner would leave the database.

## The six invariants

1. **No cross-account netting, anywhere.** No function in this codebase
   combines two accounts' positions, cash, or capacity into a number that
   could be fed back into `risk_constrain` or `Gatekeeper.stage`. The one
   sanctioned exception is `accounts.aggregate_report`, and it is
   structurally prevented from being anything else (see invariant 6).
2. **One `BrokerAdapter` instance per account, each with its own
   `BrokerCredentials`.** `BrokerAdapter.__init__` raises `CrossAccountError`
   if the credentials it's given belong to a different account_id than the
   one it was constructed with — two accounts' wiring crossed at
   construction time fails immediately rather than trading account A on
   account B's credentials.
3. **Capability policy is per account**, because an IRA has no margin.
   `TradeCapabilityPolicy` itself is not account-scoped as a type (see
   "Decisions" below) — this is enforced by which policy instance an
   account's `Gatekeeper`/adapter is constructed with, not by a field on
   the policy.
4. **The day-trade counter is per account.** `DayTradeGuard` gained
   `account_id` as a required field, and `reconcile()` raises
   `CrossAccountError` if handed a snapshot for a different account —
   two accounts at the same broker have independent PDT budgets, so a
   session_date alone can no longer identify a row (reflected in the
   composite primary key in `002_multi_account.sql`).
5. **Tax logic is account-aware, structurally, not by convention.**
   `tax.classify()` checks `account_type.is_retirement` first and returns
   `NOT_APPLICABLE` unconditionally for Roth/Traditional IRA — proven by a
   test that constructs a loss-plus-repurchase profile that WOULD flag a
   wash sale under the taxable branch and confirming the retirement branch
   never reaches that arithmetic at all.
6. **Aggregate reporting is not aggregate risk.** `aggregate_report` takes a
   list of `AccountReport` (a distinct, reporting-only type) and returns a
   plain `dict` — deliberately never a `PortfolioState`. This isn't a
   naming convention: `risk_constrain` and `Gatekeeper.stage` both require
   a `PortfolioState` and would fail immediately if handed the dict
   instead, so the aggregate-vs-per-account line is enforced by the type
   system rather than left as a comment someone could miss.

## Decisions the plan doesn't cover

- **No new `PositionLot` or `Order` Python classes.** `Lot` and the pipeline's
  `StagedOrder` gained an `account_id` field rather than being split into
  per-account subclasses — the field is the dimension, not the type.
- **`TradeCapabilityPolicy` and `RiskPolicy` are not account-scoped as
  entities.** Multiple accounts can share a policy object (e.g. two taxable
  accounts under the same risk profile); account-specific policy is a
  matter of which instance a `Gatekeeper` is constructed with, not a field
  threaded through the policy dataclasses themselves.
- **`ApprovalRequest`, `ApprovalToken`, `OpportunityEvent`, and
  `EarlyExitRequest` do not yet have `account_id`.** This is a known,
  disclosed gap, not an oversight — `002_multi_account.sql` says so
  explicitly rather than silently altering those tables. Approval and event
  flow haven't been exercised against more than one account yet; giving
  them an `account_id` before that happens would mean guessing a shape
  ahead of the work that should determine it.
- **No transfer or rebalance-across-accounts feature.** Nothing in this
  addendum adds a way to move a position or cash between accounts. That is
  a deliberate scope cut: the invariants above are about accounts never
  bleeding into each other's numbers, not about a feature that would move
  assets between them.
- **`BrokerCredentials` is a reference, never a secret** — the same
  principle as v1.0 §8.1's OS-keychain entries. `secret_ref` is a keychain
  entry name; the raw credential is never held in this dataclass or logged
  alongside it.

## What's still open

- The approval/event `account_id` gap above.
- `TradeCapabilityPolicy` per-account wiring is currently a construction-time
  convention (pass the right policy instance to the right `Gatekeeper`) —
  nothing yet asserts at startup that an IRA's adapter was actually given a
  no-margin policy. That's a candidate for a startup-time check, not
  something this addendum resolves on its own.
