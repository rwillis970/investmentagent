-- T4 unit Commit 5 (§3.3, Appendix C.3, 2026-08-01): agent.analysis_result --
-- one row per T4 analysis call, linked to the agent.opportunity_event that
-- triggered it. See agent/entities.py's own AnalysisResult docstring for
-- the full field-by-field reasoning, including why this is a DIFFERENT
-- table from the pre-existing, still-unused agent.extraction/agent.document
-- tables in 001_init.sql (a doc-keyed cache row, not an event-linked result
-- record) -- this commit does not repurpose or touch those.

CREATE TABLE agent.analysis_result (
  result_id      TEXT PRIMARY KEY,
  event_id       TEXT NOT NULL REFERENCES agent.opportunity_event(event_id),
  symbol         TEXT NOT NULL,
  model_id       TEXT NOT NULL,
  prompt_version TEXT NOT NULL,
  schema_version TEXT NOT NULL,
  doc_sha256     TEXT NOT NULL,
  cache_hit      BOOLEAN NOT NULL DEFAULT FALSE,
  cost_usd       NUMERIC(10,6) NOT NULL,
  confidence     NUMERIC(8,4) NOT NULL,
  analysis       JSONB NOT NULL,
  analyzed_at    TIMESTAMPTZ NOT NULL
);
