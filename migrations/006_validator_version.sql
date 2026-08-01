-- Review round 2 (2026-08-01): `agent.analysis_output`'s own schema/
-- citation/period-attribution validation logic can change independently
-- of a prompt, a model, or an output schema -- fixing a bug in the
-- period-attribution heuristic, or widening/narrowing its lookback
-- window, changes what gets refused without touching PROMPT_VERSION/
-- model_id/SCHEMA_VERSION at all. `validator_version` is added as a fifth
-- identity component so a cached refusal produced under an OLDER
-- validator is not served forever once the validator itself changes. See
-- agent/analysis_cache.py's own CacheKey docstring and agent/
-- analysis_output.py's VALIDATOR_VERSION docstring.

-- agent.extraction: validator_version PARTICIPATES IN THE KEY (it is part
-- of agent.analysis_cache.CacheKey, which this table durably backs via
-- agent.extraction_store.ExtractionCacheStore) -- the primary key is
-- widened to include it, so two rows differing only in validator_version
-- are correctly treated as distinct cache entries, not a collision.
ALTER TABLE agent.extraction ADD COLUMN validator_version TEXT NOT NULL DEFAULT 't4-validator-v1';
ALTER TABLE agent.extraction DROP CONSTRAINT extraction_pkey;
ALTER TABLE agent.extraction ADD PRIMARY KEY (doc_hash, prompt_version, model_id, schema_version, validator_version);
ALTER TABLE agent.extraction ALTER COLUMN validator_version DROP DEFAULT;

-- agent.analysis_result: validator_version is INFORMATIONAL ONLY here --
-- result_id alone already uniquely identifies a row (agent.
-- analysis_result_store.AnalysisResultStore assigns it internally), so no
-- primary key change is needed. Recorded so a reconstructor months later
-- knows exactly which validator build accepted this analysis, completing
-- the three version stamps (prompt_version, schema_version,
-- validator_version) alongside model_id.
ALTER TABLE agent.analysis_result ADD COLUMN validator_version TEXT NOT NULL DEFAULT 't4-validator-v1';
ALTER TABLE agent.analysis_result ALTER COLUMN validator_version DROP DEFAULT;
