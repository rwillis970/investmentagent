-- Day 1–2 schema (§9.1). Two write domains, deliberately separated:
--   * agent_*   : the running system's own records
--   * policy_*  : configuration and policy, which no model-originated
--                 artefact may write (§7.2). Grant accordingly.

CREATE SCHEMA IF NOT EXISTS agent;
CREATE SCHEMA IF NOT EXISTS policy;

-- ---------------------------------------------------------------- evidence
CREATE TABLE agent.fact (
  fact_id          BIGSERIAL PRIMARY KEY,
  entity_id        TEXT        NOT NULL,
  field            TEXT        NOT NULL,
  value            JSONB       NOT NULL,
  observed_at      TIMESTAMPTZ NOT NULL,
  effective_at     TIMESTAMPTZ NOT NULL,
  source_id        TEXT        NOT NULL,
  source_doc_hash  TEXT,
  inserted_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX fact_lookup ON agent.fact (entity_id, field, observed_at DESC);
-- Append-only: no UPDATE or DELETE grant is issued on this table.

CREATE TABLE agent.document (
  doc_hash      TEXT PRIMARY KEY,
  entity_id     TEXT,
  doc_type      TEXT        NOT NULL,
  published_at  TIMESTAMPTZ NOT NULL,
  retrieved_at  TIMESTAMPTZ NOT NULL,
  uri           TEXT,
  byte_len      INTEGER
);

CREATE TABLE agent.extraction (
  doc_hash        TEXT NOT NULL REFERENCES agent.document(doc_hash),
  prompt_version  TEXT NOT NULL,
  model_id        TEXT NOT NULL,
  schema_version  TEXT NOT NULL,
  payload         JSONB,
  tokens_in       INTEGER,
  tokens_out      INTEGER,
  cost_usd        NUMERIC(10,6),
  status          TEXT NOT NULL,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (doc_hash, prompt_version, model_id, schema_version)
);

-- ---------------------------------------------------------------- policy
CREATE TABLE policy.trade_capability (
  version           TEXT PRIMARY KEY,
  asset_class       JSONB NOT NULL,
  side              JSONB NOT NULL,
  funding           JSONB NOT NULL,
  order_type        JSONB NOT NULL,
  session           JSONB NOT NULL,
  time_in_force     JSONB NOT NULL,
  symbol_blocklist  TEXT[] NOT NULL DEFAULT '{}',
  effective_at      TIMESTAMPTZ NOT NULL,
  approved_by       TEXT NOT NULL,
  approval_evidence TEXT
);

CREATE TABLE policy.holding (
  version                 TEXT PRIMARY KEY,
  minimum_holding_period  TEXT NOT NULL,
  cooldown_period         TEXT NOT NULL,
  early_exit_categories   TEXT[] NOT NULL,
  effective_at            TIMESTAMPTZ NOT NULL
);

CREATE TABLE policy.risk (
  version                       TEXT PRIMARY KEY,
  max_position_pct              NUMERIC(5,2) NOT NULL,
  max_sector_pct                NUMERIC(5,2) NOT NULL,
  min_settled_cash_pct_of_nlv   NUMERIC(5,2) NOT NULL,
  min_absolute_settled_cash     NUMERIC(12,2) NOT NULL,
  drawdown_pause_pct            NUMERIC(5,2) NOT NULL,
  effective_at                  TIMESTAMPTZ NOT NULL
);

CREATE TABLE policy.capability_change_request (
  request_id    TEXT PRIMARY KEY,
  dimension     TEXT NOT NULL,
  from_status   TEXT NOT NULL,
  to_status     TEXT NOT NULL,
  prerequisites JSONB,
  test_results  JSONB,
  cost_impact   NUMERIC(12,2),
  approved_by   TEXT,
  approved_at   TIMESTAMPTZ
);

-- ---------------------------------------------------------------- runtime
CREATE TABLE agent.run_manifest (
  run_id                   TEXT PRIMARY KEY,
  as_of                    TIMESTAMPTZ NOT NULL,
  trigger                  TEXT NOT NULL CHECK (trigger IN ('EVENT','ROUTINE','REVIEW')),
  mode                     TEXT NOT NULL,
  code_commit              TEXT NOT NULL,
  cadence_config_version   TEXT NOT NULL,
  holding_policy_version   TEXT NOT NULL REFERENCES policy.holding(version),
  capability_policy_version TEXT NOT NULL REFERENCES policy.trade_capability(version),
  risk_policy_version      TEXT NOT NULL REFERENCES policy.risk(version),
  playbook_version         TEXT NOT NULL,
  threshold_version        TEXT NOT NULL,
  prompt_versions          TEXT[] NOT NULL,
  model_ids                TEXT[] NOT NULL,
  store_watermark          TIMESTAMPTZ NOT NULL
);

CREATE TABLE agent.opportunity_event (
  event_id          TEXT PRIMARY KEY,
  type              TEXT NOT NULL,
  source_id         TEXT NOT NULL,
  observed_at       TIMESTAMPTZ NOT NULL,
  effective_at      TIMESTAMPTZ NOT NULL,
  symbols           TEXT[] NOT NULL,
  materiality_score NUMERIC(8,4) NOT NULL,
  score_components  JSONB NOT NULL,
  threshold_version TEXT NOT NULL,
  analysis_status   TEXT NOT NULL,
  suppressed_reason TEXT
);

CREATE TABLE agent.approval_request (
  request_id           TEXT PRIMARY KEY,
  run_id               TEXT NOT NULL REFERENCES agent.run_manifest(run_id),
  proposal_snapshot    JSONB NOT NULL,
  risk_result          JSONB NOT NULL,
  price_at_analysis    NUMERIC(14,4) NOT NULL,
  price_band_low       NUMERIC(14,4) NOT NULL,
  price_band_high      NUMERIC(14,4) NOT NULL,
  shown_at             TIMESTAMPTZ NOT NULL,
  expires_at           TIMESTAMPTZ NOT NULL,
  decision             TEXT,
  decided_by           TEXT,
  decided_at           TIMESTAMPTZ,
  decision_elapsed_ms  INTEGER,
  invalidated_reason   TEXT
);

CREATE TABLE agent.approval_token (
  token_id          TEXT PRIMARY KEY,
  request_id        TEXT NOT NULL REFERENCES agent.approval_request(request_id),
  order_fingerprint TEXT NOT NULL,
  price_band_low    NUMERIC(14,4) NOT NULL,
  price_band_high   NUMERIC(14,4) NOT NULL,
  expires_at        TIMESTAMPTZ NOT NULL,
  consumed_at       TIMESTAMPTZ,
  UNIQUE (order_fingerprint, request_id)
);

CREATE TABLE agent."order" (
  order_id          TEXT PRIMARY KEY,
  run_id            TEXT NOT NULL REFERENCES agent.run_manifest(run_id),
  token_id          TEXT REFERENCES agent.approval_token(token_id),
  environment       TEXT NOT NULL CHECK (environment IN ('PAPER','LIVE')),
  client_order_id   TEXT NOT NULL UNIQUE,
  broker_order_id   TEXT,
  symbol            TEXT NOT NULL,
  side              TEXT NOT NULL,
  qty               NUMERIC(18,8) NOT NULL,
  order_type        TEXT NOT NULL,
  time_in_force     TEXT NOT NULL,
  limit_price       NUMERIC(14,4),
  status            TEXT NOT NULL,
  filled_qty        NUMERIC(18,8) NOT NULL DEFAULT 0,
  avg_fill_price    NUMERIC(14,4),
  fees              NUMERIC(12,4) NOT NULL DEFAULT 0,
  submitted_at      TIMESTAMPTZ,
  -- §12 criterion 13: a live order must carry a token.
  CONSTRAINT live_orders_require_a_token
    CHECK (environment = 'PAPER' OR token_id IS NOT NULL)
);

CREATE TABLE agent.position_lot (
  lot_id                  TEXT PRIMARY KEY,
  symbol                  TEXT NOT NULL,
  opening_order_id        TEXT REFERENCES agent."order"(order_id),
  opened_at               TIMESTAMPTZ NOT NULL,
  qty                     NUMERIC(18,8) NOT NULL,
  cost_basis              NUMERIC(14,4) NOT NULL,
  settles_at              TIMESTAMPTZ,
  minimum_holding_period  TEXT NOT NULL,
  earliest_normal_exit_at TIMESTAMPTZ NOT NULL,
  holding_policy_version  TEXT NOT NULL REFERENCES policy.holding(version),
  closed_at               TIMESTAMPTZ,
  realised_gain           NUMERIC(14,4),
  term                    TEXT CHECK (term IN ('SHORT','LONG')),
  wash_sale_flag          BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE TABLE agent.early_exit_request (
  request_id        TEXT PRIMARY KEY,
  lot_id            TEXT NOT NULL REFERENCES agent.position_lot(lot_id),
  category          TEXT NOT NULL,
  evidence_fact_ref BIGINT REFERENCES agent.fact(fact_id),
  remaining_hold    INTERVAL NOT NULL,
  approval_id       TEXT REFERENCES agent.approval_request(request_id),
  outcome           TEXT NOT NULL,
  requested_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE agent.day_trade_counter (
  session_date          DATE PRIMARY KEY,
  round_trips           JSONB NOT NULL DEFAULT '[]',
  rolling_count         INTEGER NOT NULL DEFAULT 0,
  broker_reported_count INTEGER,
  reconciled_at         TIMESTAMPTZ
);

CREATE TABLE agent.cost_ledger (
  entry_id       BIGSERIAL PRIMARY KEY,
  provider       TEXT NOT NULL,
  operation      TEXT NOT NULL,
  units          INTEGER NOT NULL,
  estimated_cost NUMERIC(10,6) NOT NULL,
  actual_cost    NUMERIC(10,6),
  budget_period  TEXT NOT NULL,
  run_id         TEXT REFERENCES agent.run_manifest(run_id),
  cache_hit      BOOLEAN NOT NULL DEFAULT FALSE,
  at             TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE agent.playbook_candidate (
  candidate_id      TEXT PRIMARY KEY,
  parent_version    TEXT NOT NULL,
  class             CHAR(1) NOT NULL CHECK (class IN ('A','B')),
  change_set        JSONB NOT NULL,
  hypothesis        TEXT NOT NULL,
  decision_rule     TEXT NOT NULL,
  registered_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  evaluation_results JSONB,
  shadow_status     TEXT,
  approved_by       TEXT,
  approved_at       TIMESTAMPTZ
);

-- ---------------------------------------------------------------- audit
CREATE TABLE agent.audit_event (
  seq            BIGSERIAL PRIMARY KEY,
  actor          TEXT NOT NULL,
  action         TEXT NOT NULL,
  object_type    TEXT NOT NULL,
  object_id      TEXT NOT NULL,
  before         JSONB,
  after          JSONB,
  correlation_id TEXT,
  ts             TIMESTAMPTZ NOT NULL DEFAULT now(),
  prev_hash      CHAR(64) NOT NULL,
  hash           CHAR(64) NOT NULL UNIQUE
);
-- Append-only. Verified from genesis at every startup (§8.1).
