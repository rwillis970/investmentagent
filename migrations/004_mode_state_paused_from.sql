-- PAUSED needs to record the mode it was paused FROM (§9.2 topology fix --
-- see agent/mode.py's own module docstring). PAUSED is not a position in
-- the escalation chain (policy.mode_state's own rows already model any
-- mode value, ordered only by seq/changed_at); this column is populated
-- only on rows where mode = 'PAUSED', and is NULL for every other row.
--
-- Without this, PAUSED's only legal one-step exit was derived from tuple
-- adjacency (the next mode alphabetically/positionally after it), which
-- made PAUSED a dead end for any system paused from DISABLED, RESEARCH or
-- PAPER, and separately made DISABLED -> PAUSED -> PRODUCTION_ACTIVE an
-- unintended two-hop bypass of the one-step escalation rule. Resuming is
-- now defined as returning to this specific value, not the next chain
-- index.

ALTER TABLE policy.mode_state ADD COLUMN paused_from TEXT;
