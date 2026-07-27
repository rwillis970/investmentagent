-- Durable mode persistence (§7.2, §9.2, §11 Day 1). §7.2 lists mode state
-- among the fields no candidate, playbook or model output may alter, and
-- requires those fields to "live in a separate schema with a separate
-- write path" from anything the optimiser's database role can reach.
-- policy.mode_state joins policy.trade_capability, policy.holding and
-- policy.risk (001_init.sql) -- the other fields §7.2 already protects the
-- same way -- rather than living in the agent schema alongside fact,
-- audit_event, order and everything else.
--
-- Append-only: no UPDATE or DELETE grant is issued on this table, matching
-- every other durable store in this codebase. The current mode is the row
-- with the greatest seq (equivalently, the greatest changed_at); there is
-- no separate "current mode" table or column to fall out of sync with the
-- history.

CREATE TABLE policy.mode_state (
  seq         BIGINT PRIMARY KEY,
  mode        TEXT NOT NULL,
  changed_at  TIMESTAMPTZ NOT NULL,
  reason      TEXT
);
CREATE INDEX mode_state_latest ON policy.mode_state (changed_at DESC);
