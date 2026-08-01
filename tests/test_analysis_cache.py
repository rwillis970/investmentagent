"""agent/analysis_cache.py (§3.3 Appendix C.3, T4 unit Commit 4): the
extraction cache -- "the same document is never paid for twice". Keyed
exactly per Appendix C.3's own spec: sha256(doc) + prompt_version +
model_id + schema_version. In-memory only, same disclosed limitation as
agent.cost.CostLedger itself (also never persisted) -- not built here.
"""
from __future__ import annotations

from agent.analysis_cache import CachedRefusal, CacheKey, ExtractionCache
from agent.analysis_output import AnalysisOutput, Claim

OUTPUT = AnalysisOutput(
    bull_case=(Claim(text="x", citations=("abc",)),),
    bear_case=(Claim(text="y", citations=("def",)),),
    contradicting_evidence=(),
    confidence=0.5,
)


def key(**over):
    base = dict(doc_sha256="a" * 64, prompt_version="t4-prompt-v1",
               model_id="claude-sonnet-5", schema_version="t4-schema-v1")
    base.update(over)
    return CacheKey(**base)


def test_miss_returns_none():
    cache = ExtractionCache()
    assert cache.get(key()) is None


def test_put_then_get_returns_the_same_output():
    cache = ExtractionCache()
    cache.put(key(), OUTPUT)
    assert cache.get(key()) is OUTPUT


def test_a_different_doc_sha256_is_a_different_key():
    cache = ExtractionCache()
    cache.put(key(), OUTPUT)
    assert cache.get(key(doc_sha256="b" * 64)) is None


def test_a_different_prompt_version_is_a_different_key():
    """Given the collision-proof cache key, changing the prompt template
    (even for the same document) must not reuse a stale cached analysis."""
    cache = ExtractionCache()
    cache.put(key(), OUTPUT)
    assert cache.get(key(prompt_version="t4-prompt-v2")) is None


def test_a_different_model_id_is_a_different_key():
    cache = ExtractionCache()
    cache.put(key(), OUTPUT)
    assert cache.get(key(model_id="claude-opus-5")) is None


def test_a_different_schema_version_is_a_different_key():
    cache = ExtractionCache()
    cache.put(key(), OUTPUT)
    assert cache.get(key(schema_version="t4-schema-v2")) is None


def test_cache_key_is_hashable_and_usable_as_a_dict_key():
    k1 = key()
    k2 = key()
    assert k1 == k2
    assert hash(k1) == hash(k2)
    d = {k1: "value"}
    assert d[k2] == "value"


# ------------------------------------------------------ cached refusals
# (review finding, 2026-08-01): a deterministic refusal -- a period-
# attribution failure, an empty bear case -- is a valid, reproducible
# result for one exact CacheKey, cached the same way a successful
# AnalysisOutput is, so re-screening the same document does not pay for
# the model again. See agent/analysis.py's own module docstring for why
# a MalformedResponse (non-JSON reply) is deliberately NEVER put here.

def test_put_refusal_then_get_returns_the_cached_refusal():
    cache = ExtractionCache()
    refusal = CachedRefusal(message="bear_case must be non-empty", tokens_in=10,
                            tokens_out=5, cost_usd=0.001)
    cache.put_refusal(key(), refusal)
    assert cache.get(key()) is refusal


def test_a_cached_refusal_and_a_cached_output_are_distinguishable_by_type():
    cache = ExtractionCache()
    cache.put(key(doc_sha256="e" * 64), OUTPUT)
    cache.put_refusal(key(doc_sha256="f" * 64), CachedRefusal("x", 1, 1, 0.0))
    assert isinstance(cache.get(key(doc_sha256="e" * 64)), AnalysisOutput)
    assert isinstance(cache.get(key(doc_sha256="f" * 64)), CachedRefusal)


def test_a_cached_refusal_follows_the_same_key_collision_rules_as_an_output():
    cache = ExtractionCache()
    cache.put_refusal(key(), CachedRefusal("x", 1, 1, 0.0))
    assert cache.get(key(prompt_version="t4-prompt-v2")) is None
    assert cache.get(key(model_id="claude-opus-5")) is None
    assert cache.get(key(schema_version="t4-schema-v2")) is None
    assert cache.get(key(doc_sha256="b" * 64)) is None


def test_truncated_documents_get_a_different_key_via_their_own_sha256():
    """See agent.edgar.FilingDocumentFetch's own docstring: sha256 is
    computed over the ACTUAL STORED (possibly truncated) bytes, so two
    fetches of the same underlying document truncated at different points
    already produce different doc_sha256 values here -- this test just
    confirms this cache treats them as genuinely distinct, not merges them."""
    cache = ExtractionCache()
    full_key = key(doc_sha256="c" * 64)
    truncated_key = key(doc_sha256="d" * 64)
    cache.put(full_key, OUTPUT)
    assert cache.get(truncated_key) is None
