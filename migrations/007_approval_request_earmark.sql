-- Earmarking unit (2026-08-02): a pending BUY approval request now reserves
-- the settled cash its order would consume, so a second pending request
-- against the same account is sized against what actually remains, not
-- against a world where the first request does not exist. See
-- agent/risk.py's PortfolioState.pending_buy_notional -- documented in
-- Change Request §6.1 from the start, but never actually wired to anything
-- until this unit -- and agent/approval_request_store.py's own module
-- docstring for the full reasoning.
--
-- `earmark` is 0 for a SELL/CLOSE request (a close frees cash; it reserves
-- nothing) and the order's own authorized notional (`StagedOrder.notional`)
-- for a BUY.
ALTER TABLE agent.approval_request ADD COLUMN earmark NUMERIC(14,4) NOT NULL DEFAULT 0;
ALTER TABLE agent.approval_request ALTER COLUMN earmark DROP DEFAULT;
