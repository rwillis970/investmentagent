-- Multi-account addendum (see docs/multi-account-addendum.md). Additive only:
-- 001_init.sql is untouched. account_id becomes a first-class dimension on
-- the entities that need it; TradeCapabilityPolicy and RiskPolicy do NOT gain
-- one at the entity level (see the addendum's "decisions" section for why).

CREATE TABLE agent.account (
  account_id    TEXT PRIMARY KEY,
  account_type  TEXT NOT NULL CHECK (account_type IN ('TAXABLE','ROTH_IRA','TRADITIONAL_IRA')),
  broker        TEXT NOT NULL,
  key_id        TEXT NOT NULL,
  secret_ref    TEXT NOT NULL          -- a keychain reference, never a raw secret
);

ALTER TABLE agent.position_lot
  ADD COLUMN account_id TEXT NOT NULL REFERENCES agent.account(account_id);

ALTER TABLE agent."order"
  ADD COLUMN account_id TEXT NOT NULL REFERENCES agent.account(account_id);

ALTER TABLE agent.run_manifest
  ADD COLUMN account_id TEXT NOT NULL REFERENCES agent.account(account_id);

-- The day-trade counter's PK becomes composite: two accounts at the same
-- broker have independent PDT budgets, so a session_date alone can no longer
-- identify a row.
ALTER TABLE agent.day_trade_counter DROP CONSTRAINT day_trade_counter_pkey;
ALTER TABLE agent.day_trade_counter
  ADD COLUMN account_id TEXT NOT NULL REFERENCES agent.account(account_id);
ALTER TABLE agent.day_trade_counter
  ADD PRIMARY KEY (account_id, session_date);

-- Deliberately NOT altered here -- noted as a gap in the addendum, not a
-- silent omission: approval_request, approval_token, opportunity_event and
-- early_exit_request do not yet have account_id. Approval and event flow
-- haven't been exercised against more than one account yet; this is the
-- next thing that work touches, not something this migration should guess
-- the shape of.
