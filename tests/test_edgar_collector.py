"""agent/edgar_collector.py (§2, §11 Day 4 collectors unit, Commit 2). No
test here makes a network call -- every EdgarClient is bound to a
ScriptedTransport, same discipline as tests/test_edgar.py.
"""
from __future__ import annotations

import hashlib
import inspect
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agent.broker.transport import ScriptedTransport
from agent.edgar import EdgarClient
from agent.edgar_collector import (FIELD, FIELD_DOCUMENT, SOURCE_ID,
                                   EdgarCollectorError, TickerCikCache,
                                   collect_filing_document, collect_filings)
from agent.store import FactStore

FIXTURES = Path(__file__).parent.parent / "scripts" / "fixtures" / "edgar"

UA = "InvestmentAgent Pilot test@example.com"
T0 = datetime(2026, 7, 31, 15, 0, tzinfo=timezone.utc)


def client(transport):
    return EdgarClient(user_agent=UA, transport=transport, http_timeout_seconds=1.0,
                       http_max_retries=1, min_request_interval_seconds=0.001,
                       sleep_fn=lambda s: None, monotonic_fn=lambda: 0.0)


def ticker_map_body():
    return {
        "0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
    }


def submissions_body(accession="0000320193-26-000011", form="8-K", items="2.02,9.01",
                     filing_date="2026-04-30", report_date="2026-04-30",
                     accepted="2026-04-30T16:30:41.000Z"):
    return {
        "filings": {
            "recent": {
                "form": [form],
                "filingDate": [filing_date],
                "reportDate": [report_date],
                "acceptanceDateTime": [accepted],
                "accessionNumber": [accession],
                "primaryDocument": ["doc.htm"],
                "items": [items],
            },
            "files": [],
        }
    }


# --------------------------------------------------------------- TickerCikCache

def test_cache_is_stale_before_its_first_refresh():
    cache = TickerCikCache()
    assert cache.is_stale(now=T0, max_age=timedelta(hours=24))


def test_cache_refresh_populates_the_map_and_records_the_instant():
    t = ScriptedTransport()
    t.enqueue(200, ticker_map_body())
    cache = TickerCikCache()
    cache.refresh(client(t), now=T0)
    assert cache.get("AAPL") == "0000320193"
    assert cache.refreshed_at == T0


def test_cache_is_not_stale_immediately_after_a_refresh():
    t = ScriptedTransport()
    t.enqueue(200, ticker_map_body())
    cache = TickerCikCache()
    cache.refresh(client(t), now=T0)
    assert not cache.is_stale(now=T0 + timedelta(hours=1), max_age=timedelta(hours=24))


def test_cache_becomes_stale_once_max_age_elapses():
    t = ScriptedTransport()
    t.enqueue(200, ticker_map_body())
    cache = TickerCikCache()
    cache.refresh(client(t), now=T0)
    assert cache.is_stale(now=T0 + timedelta(hours=25), max_age=timedelta(hours=24))


def test_ensure_fresh_only_refetches_when_stale():
    t = ScriptedTransport()
    t.enqueue(200, ticker_map_body())
    cache = TickerCikCache()
    cache.ensure_fresh(client(t), now=T0, max_age=timedelta(hours=24))
    cache.ensure_fresh(client(t), now=T0 + timedelta(hours=1), max_age=timedelta(hours=24))
    assert len(t.calls) == 1   # second call found the cache still fresh


def test_ensure_fresh_refetches_once_stale():
    t = ScriptedTransport()
    t.enqueue(200, ticker_map_body())
    t.enqueue(200, ticker_map_body())
    cache = TickerCikCache()
    cache.ensure_fresh(client(t), now=T0, max_age=timedelta(hours=24))
    cache.ensure_fresh(client(t), now=T0 + timedelta(hours=25), max_age=timedelta(hours=24))
    assert len(t.calls) == 2


# ---------------------------------------------------------------- collect_filings

def test_collect_filings_rejects_a_naive_now():
    with pytest.raises(EdgarCollectorError):
        collect_filings(client(ScriptedTransport()), FactStore(), TickerCikCache(), ["AAPL"],
                        now=datetime(2026, 7, 31), ticker_cik_refresh_max_age=timedelta(hours=24))


def test_collect_filings_skips_a_symbol_with_no_cik():
    t = ScriptedTransport()
    t.enqueue(200, ticker_map_body())   # no MSFT in the map
    store = FactStore()
    result = collect_filings(client(t), store, TickerCikCache(), ["MSFT"],
                             now=T0, ticker_cik_refresh_max_age=timedelta(hours=24))
    assert result.facts == ()
    assert "MSFT" in result.skipped
    assert "no CIK" in result.skipped["MSFT"]


def test_collect_filings_writes_one_fact_per_new_filing():
    t = ScriptedTransport()
    t.enqueue(200, ticker_map_body())
    t.enqueue(200, submissions_body())
    store = FactStore()
    result = collect_filings(client(t), store, TickerCikCache(), ["AAPL"],
                             now=T0, ticker_cik_refresh_max_age=timedelta(hours=24))
    assert len(result.facts) == 1
    fact = result.facts[0]
    assert fact.entity_id == "AAPL"
    assert fact.field == FIELD
    assert fact.source_id == SOURCE_ID
    assert fact.value["form"] == "8-K"
    assert fact.value["item_codes"] == ["2.02", "9.01"]
    assert fact.value["accession_number"] == "0000320193-26-000011"
    assert fact.source_doc_hash == "0000320193-26-000011"
    assert len(store) == 1


def test_observed_at_is_the_acceptance_datetime_when_present():
    t = ScriptedTransport()
    t.enqueue(200, ticker_map_body())
    t.enqueue(200, submissions_body(accepted="2026-04-30T16:30:41.000Z"))
    store = FactStore()
    result = collect_filings(client(t), store, TickerCikCache(), ["AAPL"],
                             now=T0, ticker_cik_refresh_max_age=timedelta(hours=24))
    assert result.facts[0].observed_at == datetime(2026, 4, 30, 16, 30, 41, tzinfo=timezone.utc)


def test_observed_at_falls_back_to_end_of_day_eastern_when_acceptance_missing():
    """The safe direction for a look-ahead guard: erring LATE, not early --
    see module docstring's OBSERVED_AT section."""
    t = ScriptedTransport()
    t.enqueue(200, ticker_map_body())
    t.enqueue(200, submissions_body(accepted=None, filing_date="2026-04-30"))
    store = FactStore()
    result = collect_filings(client(t), store, TickerCikCache(), ["AAPL"],
                             now=T0, ticker_cik_refresh_max_age=timedelta(hours=24))
    observed = result.facts[0].observed_at
    # 23:59:59 America/New_York on 2026-04-30 (EDT, UTC-4) is 2026-05-01 03:59:59Z
    assert observed == datetime(2026, 5, 1, 3, 59, 59, tzinfo=timezone.utc)


def test_effective_at_is_the_report_date_not_the_filing_date():
    t = ScriptedTransport()
    t.enqueue(200, ticker_map_body())
    t.enqueue(200, submissions_body(report_date="2026-03-28", filing_date="2026-05-01"))
    store = FactStore()
    result = collect_filings(client(t), store, TickerCikCache(), ["AAPL"],
                             now=T0, ticker_cik_refresh_max_age=timedelta(hours=24))
    assert result.facts[0].effective_at == datetime(2026, 3, 28, tzinfo=timezone.utc)


def test_effective_at_falls_back_to_filing_date_when_report_date_missing():
    t = ScriptedTransport()
    t.enqueue(200, ticker_map_body())
    t.enqueue(200, submissions_body(report_date=None, filing_date="2026-05-01"))
    store = FactStore()
    result = collect_filings(client(t), store, TickerCikCache(), ["AAPL"],
                             now=T0, ticker_cik_refresh_max_age=timedelta(hours=24))
    assert result.facts[0].effective_at == datetime(2026, 5, 1, tzinfo=timezone.utc)


def test_a_second_cycle_does_not_re_write_the_same_accession_number():
    t = ScriptedTransport()
    t.enqueue(200, ticker_map_body())
    t.enqueue(200, submissions_body())
    store = FactStore()
    cache = TickerCikCache()
    collect_filings(client(t), store, cache, ["AAPL"],
                    now=T0, ticker_cik_refresh_max_age=timedelta(hours=24))
    assert len(store) == 1

    t.enqueue(200, submissions_body())   # EDGAR reports the SAME filing again
    result2 = collect_filings(client(t), store, cache, ["AAPL"],
                              now=T0 + timedelta(minutes=5),
                              ticker_cik_refresh_max_age=timedelta(hours=24))
    assert result2.facts == ()
    assert len(store) == 1   # no duplicate row


def test_a_genuinely_new_filing_on_a_later_cycle_is_written():
    t = ScriptedTransport()
    t.enqueue(200, ticker_map_body())
    t.enqueue(200, submissions_body(accession="0000320193-26-000011"))
    store = FactStore()
    cache = TickerCikCache()
    collect_filings(client(t), store, cache, ["AAPL"],
                    now=T0, ticker_cik_refresh_max_age=timedelta(hours=24))

    t.enqueue(200, submissions_body(accession="0000320193-26-000099"))
    result2 = collect_filings(client(t), store, cache, ["AAPL"],
                              now=T0 + timedelta(minutes=5),
                              ticker_cik_refresh_max_age=timedelta(hours=24))
    assert len(result2.facts) == 1
    assert result2.facts[0].value["accession_number"] == "0000320193-26-000099"
    assert len(store) == 2


def test_look_ahead_guard_hides_a_filing_before_its_own_observed_at():
    t = ScriptedTransport()
    t.enqueue(200, ticker_map_body())
    t.enqueue(200, submissions_body(accepted="2026-04-30T16:30:41.000Z"))
    store = FactStore()
    collect_filings(client(t), store, TickerCikCache(), ["AAPL"],
                    now=T0, ticker_cik_refresh_max_age=timedelta(hours=24))

    after = store.as_of(datetime(2026, 4, 30, 16, 30, 42, tzinfo=timezone.utc))
    before = store.as_of(datetime(2026, 4, 30, 16, 30, 40, tzinfo=timezone.utc))
    assert after.get("AAPL", FIELD) is not None
    assert before.get("AAPL", FIELD) is None


# ----------------------- collect_filing_document (T4 prerequisite unit)
# FETCH IS NOT AUTOMATIC (module docstring's own FETCH IS NOT AUTOMATIC
# section): this is a per-filing, explicitly-invoked fetch -- never called
# by collect_filings()'s periodic metadata sweep.

def test_collect_filings_never_fetches_a_document_body():
    """collect_filings()'s own ScriptedTransport queue only ever has
    metadata-shaped (JSON) responses enqueued -- if it tried to also fetch
    a document body, it would call `request_raw` against a transport with
    nothing raw enqueued, and fail loudly (see ScriptedTransport's own
    'wrong queue shape' assertion) rather than silently doing extra work."""
    t = ScriptedTransport()
    t.enqueue(200, ticker_map_body())
    t.enqueue(200, submissions_body())
    store = FactStore()
    collect_filings(client(t), store, TickerCikCache(), ["AAPL"],
                    now=T0, ticker_cik_refresh_max_age=timedelta(hours=24))
    # Exactly the 2 metadata calls above -- no request_raw call was made.
    assert len(t.calls) == 2


def test_collect_filing_document_stores_a_distinct_fact_kind():
    t = ScriptedTransport()
    t.enqueue_raw(200, b"<html>real filing body</html>")
    store = FactStore()
    fact = collect_filing_document(
        client(t), store, "AAPL", cik="320193",
        accession_number="0000320193-26-000011", primary_document="doc.htm",
        now=T0, max_bytes=1_000_000,
    )
    assert fact.field == FIELD_DOCUMENT
    assert fact.field != FIELD   # a distinct kind -- never confusable with metadata
    assert fact.entity_id == "AAPL"
    assert fact.source_id == SOURCE_ID
    assert fact.value["text"] == "<html>real filing body</html>"
    assert fact.value["accession_number"] == "0000320193-26-000011"
    assert fact.value["truncated"] is False
    view = store.now_view()
    assert view.get("AAPL", FIELD_DOCUMENT) == fact.value
    # the OLD metadata field for the same symbol is untouched/absent
    assert view.get("AAPL", FIELD) is None


def test_collect_filing_document_source_doc_hash_is_the_real_sha256():
    body = b"raw bytes to hash"
    t = ScriptedTransport()
    t.enqueue_raw(200, body)
    store = FactStore()
    fact = collect_filing_document(
        client(t), store, "AAPL", cik="320193",
        accession_number="0000320193-26-000011", primary_document="doc.htm",
        now=T0, max_bytes=1_000_000,
    )
    assert fact.source_doc_hash == hashlib.sha256(body).hexdigest()


def test_collect_filing_document_records_truncation_on_the_fact():
    t = ScriptedTransport()
    t.enqueue_raw(200, b"x" * 50, truncated=True)
    store = FactStore()
    fact = collect_filing_document(
        client(t), store, "AAPL", cik="320193",
        accession_number="0000320193-26-000011", primary_document="doc.htm",
        now=T0, max_bytes=50,
    )
    assert fact.value["truncated"] is True
    assert fact.value["byte_length"] == 50


def test_collect_filing_document_rejects_a_naive_now():
    t = ScriptedTransport()
    store = FactStore()
    with pytest.raises(EdgarCollectorError):
        collect_filing_document(
            client(t), store, "AAPL", cik="320193",
            accession_number="0000320193-26-000011", primary_document="doc.htm",
            now=datetime(2026, 7, 31), max_bytes=1000,
        )


def test_collect_filing_document_uses_the_real_url_scheme():
    t = ScriptedTransport()
    t.enqueue_raw(200, b"body")
    store = FactStore()
    collect_filing_document(
        client(t), store, "AAPL", cik="320193",
        accession_number="0000320193-26-000011", primary_document="aapl-20260430.htm",
        now=T0, max_bytes=1000,
    )
    assert t.calls[0]["path"] == ("https://www.sec.gov/Archives/edgar/data/320193/"
                                 "000032019326000011/aapl-20260430.htm")


def test_fetch_is_not_automatic_only_the_caller_decides_which_filing():
    """collect_filing_document takes accession_number/primary_document/cik
    as explicit, caller-supplied arguments -- it has no way to discover a
    filing on its own (no symbol-list sweep, unlike collect_filings). The
    caller is expected to be whatever decides a filing is worth analysing
    (the T4 trigger path, not yet built -- see module docstring)."""
    sig = inspect.signature(collect_filing_document)
    assert "symbols" not in sig.parameters
    assert {"accession_number", "primary_document", "cik"} <= set(sig.parameters)


# ------------------------------------------- real fixture, end-to-end

def test_collect_filing_document_against_the_real_committed_10k_fixture():
    body = (FIXTURES / "AAPL_10K_0000320193-25-000079.htm").read_bytes()
    t = ScriptedTransport()
    t.enqueue_raw(200, body)
    store = FactStore()
    fact = collect_filing_document(
        client(t), store, "AAPL", cik="320193",
        accession_number="0000320193-25-000079", primary_document="aapl-20250930.htm",
        now=T0, max_bytes=5_000_000,
    )
    assert fact.value["truncated"] is False
    assert fact.value["byte_length"] == len(body)
    assert fact.source_doc_hash == hashlib.sha256(body).hexdigest()
    stored_text = store.now_view().get("AAPL", FIELD_DOCUMENT)["text"]
    from agent.filing_text import extract_filing_text
    extracted = extract_filing_text(stored_text)
    assert "Products | $ | 307,003 | $ | 294,866 | $ | 298,085" in extracted
