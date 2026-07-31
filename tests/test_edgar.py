"""agent/edgar.py (§2, §11 Day 4 collectors unit, Commit 2). No test here
makes a network call -- every test injects a ScriptedTransport, the same
discipline as tests/test_broker_alpaca_market_data.py, plus a fake
sleep_fn/monotonic_fn so the rate limiter's own sleeping is asserted on
without a real test run ever waiting for it.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from agent.broker.transport import ScriptedTransport, TransportError
from agent.edgar import (ALLOWED_FORMS, EdgarClient, EdgarError, FilingDocumentFetch,
                         _document_url, _parse_item_codes, _parse_recent_filings,
                         _RateLimiter)

UA = "InvestmentAgent Pilot test@example.com"
FIXTURES = Path(__file__).parent.parent / "scripts" / "fixtures" / "edgar"


class FakeClock:
    """Deterministic replacement for time.monotonic/time.sleep."""

    def __init__(self):
        self.now = 0.0
        self.slept: list[float] = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


def edgar_client(transport=None, *, clock=None, min_interval=0.15, max_retries=1):
    clock = clock or FakeClock()
    return EdgarClient(
        user_agent=UA, transport=transport or ScriptedTransport(),
        http_timeout_seconds=1.0, http_max_retries=max_retries,
        min_request_interval_seconds=min_interval,
        sleep_fn=clock.sleep, monotonic_fn=clock.monotonic,
    ), clock


# ----------------------------------------------------------------- _RateLimiter

def test_rate_limiter_does_not_sleep_on_the_first_call():
    clock = FakeClock()
    limiter = _RateLimiter(0.15, sleep_fn=clock.sleep, monotonic_fn=clock.monotonic)
    limiter.throttle()
    assert clock.slept == []


def test_rate_limiter_sleeps_the_remaining_interval_on_a_fast_second_call():
    clock = FakeClock()
    limiter = _RateLimiter(0.15, sleep_fn=clock.sleep, monotonic_fn=clock.monotonic)
    limiter.throttle()
    clock.now += 0.05   # only 50ms elapsed
    limiter.throttle()
    assert clock.slept == [pytest.approx(0.10)]


def test_rate_limiter_does_not_sleep_if_enough_time_already_elapsed():
    clock = FakeClock()
    limiter = _RateLimiter(0.15, sleep_fn=clock.sleep, monotonic_fn=clock.monotonic)
    limiter.throttle()
    clock.now += 1.0
    limiter.throttle()
    assert clock.slept == []


def test_rate_limiter_rejects_a_non_positive_interval():
    with pytest.raises(EdgarError):
        _RateLimiter(0.0)


# ------------------------------------------------------------- item code parsing

def test_parse_item_codes_handles_a_comma_joined_string():
    assert _parse_item_codes("2.02,9.01") == ("2.02", "9.01")


def test_parse_item_codes_handles_a_json_list():
    assert _parse_item_codes(["2.02", "9.01"]) == ("2.02", "9.01")


def test_parse_item_codes_handles_empty_and_none():
    assert _parse_item_codes("") == ()
    assert _parse_item_codes(None) == ()
    assert _parse_item_codes([]) == ()


def test_parse_item_codes_rejects_an_unrecognised_shape():
    with pytest.raises(EdgarError):
        _parse_item_codes(12345)


# ------------------------------------------------------- _parse_recent_filings

def test_parse_recent_filings_uncolumns_the_parallel_arrays():
    recent = {
        "form": ["8-K", "10-Q"],
        "filingDate": ["2026-04-30", "2026-05-01"],
        "reportDate": ["2026-04-30", "2026-03-28"],
        "acceptanceDateTime": ["2026-04-30T16:30:41.000Z", "2026-05-01T06:01:00.000Z"],
        "accessionNumber": ["0000320193-26-000011", "0000320193-26-000013"],
        "primaryDocument": ["aapl-20260430.htm", "aapl-20260328.htm"],
        "items": ["2.02,9.01", ""],
    }
    parsed = _parse_recent_filings(recent)
    assert len(parsed) == 2
    assert parsed[0]["form"] == "8-K"
    assert parsed[0]["item_codes"] == ("2.02", "9.01")
    assert parsed[0]["accession_number"] == "0000320193-26-000011"
    assert parsed[1]["form"] == "10-Q"
    assert parsed[1]["item_codes"] == ()
    assert parsed[1]["report_date"] == "2026-03-28"


def test_parse_recent_filings_tolerates_missing_optional_columns():
    recent = {
        "form": ["10-K"],
        "filingDate": ["2026-01-30"],
        "accessionNumber": ["0000320193-26-000005"],
        # no reportDate / acceptanceDateTime / primaryDocument / items keys at all
    }
    parsed = _parse_recent_filings(recent)
    assert parsed[0]["report_date"] is None
    assert parsed[0]["accepted_at"] is None
    assert parsed[0]["primary_document"] is None
    assert parsed[0]["item_codes"] == ()


# ----------------------------------------------------------------- construction

def test_requires_a_user_agent_naming_a_contact_email():
    with pytest.raises(EdgarError, match="user_agent"):
        EdgarClient(user_agent="", transport=ScriptedTransport())
    with pytest.raises(EdgarError, match="user_agent"):
        EdgarClient(user_agent="just a name, no email", transport=ScriptedTransport())


# ------------------------------------------------------------------ ticker_cik_map

def test_ticker_cik_map_rekeys_by_ticker_and_zero_pads_cik():
    t = ScriptedTransport()
    t.enqueue(200, {
        "0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
        "1": {"cik_str": 789019, "ticker": "MSFT", "title": "Microsoft Corp"},
    })
    client, _ = edgar_client(t)
    result = client.ticker_cik_map()
    assert result == {"AAPL": "0000320193", "MSFT": "0000789019"}


def test_ticker_cik_map_hits_the_documented_url_with_the_user_agent_header():
    t = ScriptedTransport()
    t.enqueue(200, {})
    client, _ = edgar_client(t)
    client.ticker_cik_map()
    call = t.calls[0]
    assert call["path"] == "https://www.sec.gov/files/company_tickers.json"
    assert call["headers"]["User-Agent"] == UA


# ------------------------------------------------------------------ filings_for_cik

def _submissions_body(**over):
    base = {
        "filings": {
            "recent": {
                "form": ["8-K", "10-Q", "4"],
                "filingDate": ["2026-04-30", "2026-05-01", "2026-04-15"],
                "reportDate": ["2026-04-30", "2026-03-28", None],
                "acceptanceDateTime": [
                    "2026-04-30T16:30:41.000Z", "2026-05-01T06:01:00.000Z", None,
                ],
                "accessionNumber": [
                    "0000320193-26-000011", "0000320193-26-000013", "0000320193-26-000099",
                ],
                "primaryDocument": ["aapl-20260430.htm", "aapl-20260328.htm", "form4.xml"],
                "items": ["2.02,9.01", "", ""],
            },
            "files": [],
        }
    }
    base.update(over)
    return base


def test_filings_for_cik_filters_to_the_allowed_forms():
    t = ScriptedTransport()
    t.enqueue(200, _submissions_body())
    client, _ = edgar_client(t)
    result = client.filings_for_cik("320193")
    forms = {f["form"] for f in result}
    assert forms == {"8-K", "10-Q"}   # "4" (an ownership form) is excluded


def test_filings_for_cik_zero_pads_the_cik_in_the_url():
    t = ScriptedTransport()
    t.enqueue(200, _submissions_body())
    client, _ = edgar_client(t)
    client.filings_for_cik("320193")
    assert t.calls[0]["path"] == "https://data.sec.gov/submissions/CIK0000320193.json"


def test_filings_for_cik_fetches_older_files_when_present():
    t = ScriptedTransport()
    t.enqueue(200, _submissions_body(filings={
        "recent": _submissions_body()["filings"]["recent"],
        "files": [{"name": "CIK0000320193-submissions-001.json"}],
    }))
    t.enqueue(200, {
        "form": ["10-K"], "filingDate": ["2020-01-30"],
        "reportDate": ["2019-12-31"], "acceptanceDateTime": ["2020-01-30T20:00:00.000Z"],
        "accessionNumber": ["0000320193-20-000001"], "primaryDocument": ["old10k.htm"],
        "items": [""],
    })
    client, _ = edgar_client(t)
    result = client.filings_for_cik("320193")
    assert len(t.calls) == 2
    assert t.calls[1]["path"] == "https://data.sec.gov/submissions/CIK0000320193-submissions-001.json"
    assert any(f["accession_number"] == "0000320193-20-000001" for f in result)


def test_filings_for_cik_can_skip_older_files():
    t = ScriptedTransport()
    t.enqueue(200, _submissions_body(filings={
        "recent": _submissions_body()["filings"]["recent"],
        "files": [{"name": "CIK0000320193-submissions-001.json"}],
    }))
    client, _ = edgar_client(t)
    client.filings_for_cik("320193", include_older=False)
    assert len(t.calls) == 1


def test_filings_for_cik_can_narrow_the_forms_filter():
    t = ScriptedTransport()
    t.enqueue(200, _submissions_body())
    client, _ = edgar_client(t)
    result = client.filings_for_cik("320193", forms=frozenset({"8-K"}))
    assert {f["form"] for f in result} == {"8-K"}


# ------------------------------------------------------------------- rate limiting

def test_every_request_goes_through_the_rate_limiter():
    t = ScriptedTransport()
    t.enqueue(200, _submissions_body())
    t.enqueue(200, _submissions_body())
    client, clock = edgar_client(t, min_interval=0.15)
    client.filings_for_cik("320193")
    client.filings_for_cik("789019")
    # second call happened at simulated time 0.0 still -> throttle must have
    # slept ~0.15s before issuing it
    assert clock.slept == [pytest.approx(0.15)]


# --------------------------------------------------------------------- retries/errors

def test_reads_retry_on_transport_error_up_to_max_retries():
    t = ScriptedTransport()
    t.enqueue_error(TransportError("boom"))
    t.enqueue(200, _submissions_body())
    client, _ = edgar_client(t, max_retries=1)
    result = client.filings_for_cik("320193")
    assert len(result) == 2
    assert len(t.calls) == 2


def test_a_non_2xx_status_raises():
    t = ScriptedTransport()
    t.enqueue(404, {"error": "not found"})
    client, _ = edgar_client(t)
    with pytest.raises(EdgarError, match="404"):
        client.filings_for_cik("320193")


# ------------------------------------------------------------------------- misc

def test_allowed_forms_matches_materialitys_own_allowlist():
    from agent.materiality import WEIGHTED_FORMS
    assert ALLOWED_FORMS == frozenset({"8-K"}) | WEIGHTED_FORMS


# ------------------------------------------- filing_document (T4 prerequisite)
# CONFIRMED directly against SEC's own "Accessing EDGAR Data" page
# (sec.gov/search-filings/edgar-search-assistance/accessing-edgar-data,
# fetched 2026-07-31): "Post-EDGAR 7.0 filings... are also accessible via an
# alternative symbolic path, incorporating an intermediate accession-number
# directory without dashes" -- e.g. /Archives/edgar/data/1122304/
# 000119312515118890/0001193125-15-118890.txt. The examples on that same
# page use the CIK WITHOUT leading zeros (e.g. "51143", "1122304"), unlike
# the zero-padded 10-digit CIK `filings_for_cik` uses for the DIFFERENT
# data.sec.gov/submissions/ API.

def test_document_url_uses_unpadded_cik_and_dashless_accession():
    # Real, confirmed example: Apple's 8-K accession 0000320193-26-000011,
    # CIK 320193 (fetched directly from SEC 2026-07-31, see this unit's
    # delivery report).
    url = _document_url("0000320193", "0000320193-26-000011", "aapl-20260430.htm")
    assert url == ("https://www.sec.gov/Archives/edgar/data/320193/"
                   "000032019326000011/aapl-20260430.htm")


def test_document_url_accepts_an_already_unpadded_cik():
    url = _document_url("320193", "0000320193-26-000011", "aapl-20260430.htm")
    assert url == ("https://www.sec.gov/Archives/edgar/data/320193/"
                   "000032019326000011/aapl-20260430.htm")


def test_filing_document_fetches_via_the_archives_url_with_declaring_user_agent():
    t = ScriptedTransport()
    t.enqueue_raw(200, b"<html>filing body</html>")
    client, _ = edgar_client(t)
    result = client.filing_document("320193", "0000320193-26-000011",
                                     "aapl-20260430.htm", max_bytes=1_000_000)
    assert len(t.calls) == 1
    call = t.calls[0]
    assert call["path"] == ("https://www.sec.gov/Archives/edgar/data/320193/"
                            "000032019326000011/aapl-20260430.htm")
    assert call["headers"]["User-Agent"] == UA
    assert call["max_bytes"] == 1_000_000
    assert isinstance(result, FilingDocumentFetch)
    assert result.text == "<html>filing body</html>"
    assert result.truncated is False
    assert result.byte_length == len(b"<html>filing body</html>")


def test_filing_document_computes_sha256_over_the_actual_stored_bytes():
    body = b"some raw filing bytes \xe2\x80\x94 em dash"
    t = ScriptedTransport()
    t.enqueue_raw(200, body)
    client, _ = edgar_client(t)
    result = client.filing_document("320193", "0000320193-26-000011",
                                     "aapl-20260430.htm", max_bytes=1_000_000)
    assert result.sha256 == hashlib.sha256(body).hexdigest()


def test_filing_document_reports_truncation_and_hashes_only_the_stored_bytes():
    """sha256 is computed over what was ACTUALLY stored (possibly
    truncated), not a hypothetical full body this client never received --
    see agent/edgar_collector.py's module docstring for why this is the
    correct cache-key semantics for Commit 4's extraction cache."""
    full = b"x" * 1000
    truncated_body = full[:100]
    t = ScriptedTransport()
    t.enqueue_raw(200, truncated_body, truncated=True)
    client, _ = edgar_client(t)
    result = client.filing_document("320193", "0000320193-26-000011",
                                     "aapl-20260430.htm", max_bytes=100)
    assert result.truncated is True
    assert result.byte_length == 100
    assert result.sha256 == hashlib.sha256(truncated_body).hexdigest()
    assert result.sha256 != hashlib.sha256(full).hexdigest()


def test_filing_document_reuses_the_same_rate_limiter():
    t = ScriptedTransport()
    t.enqueue_raw(200, b"a")
    t.enqueue_raw(200, b"b")
    client, clock = edgar_client(t, min_interval=0.15)
    client.filing_document("320193", "1", "a.htm", max_bytes=100)
    client.filing_document("320193", "2", "b.htm", max_bytes=100)
    assert clock.slept == [pytest.approx(0.15)]


def test_filing_document_retries_on_transport_error():
    t = ScriptedTransport()
    t.enqueue_error(TransportError("boom"))
    t.enqueue_raw(200, b"recovered")
    client, _ = edgar_client(t, max_retries=1)
    result = client.filing_document("320193", "1", "a.htm", max_bytes=100)
    assert result.text == "recovered"
    assert len(t.calls) == 2


def test_filing_document_non_2xx_raises():
    t = ScriptedTransport()
    t.enqueue_raw(404, b"not found")
    client, _ = edgar_client(t)
    with pytest.raises(EdgarError, match="404"):
        client.filing_document("320193", "1", "missing.htm", max_bytes=100)


def test_filing_document_decodes_utf8_with_replacement_on_bad_bytes():
    t = ScriptedTransport()
    t.enqueue_raw(200, b"valid \xff\xfe invalid utf8")
    client, _ = edgar_client(t)
    result = client.filing_document("320193", "1", "a.htm", max_bytes=1000)
    assert "valid" in result.text
    assert "invalid utf8" in result.text  # decode did not raise


# ------------------------------------------------- real fixtures end-to-end

def _fixture_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def test_filing_document_against_the_real_committed_8k_fixture():
    body = _fixture_bytes("AAPL_8K_0000320193-26-000018.htm")
    t = ScriptedTransport()
    t.enqueue_raw(200, body)
    client, _ = edgar_client(t)
    result = client.filing_document("320193", "0000320193-26-000018",
                                     "aapl-20260730.htm",
                                     max_bytes=5_000_000)
    assert result.sha256 == hashlib.sha256(body).hexdigest()
    assert result.byte_length == len(body)
    assert result.truncated is False
    # `.text` here is the RAW, undecoded HTML (filing_document does not
    # extract) -- "FORM 8-K" as rendered text is not a literal substring of
    # the markup (entities/whitespace differ); "Item 2.02" survives intact
    # since it isn't broken up by any tag. See tests/test_filing_text.py
    # for the extracted, human-readable form of this same real document.
    assert "Item 2.02" in result.text
    assert "<html" in result.text


def test_filing_document_against_the_real_committed_10k_fixture_with_a_realistic_cap():
    """The real 10-K is 1,520,208 bytes -- comfortably under the configured
    5,000,000-byte default cap (agent.config.Config.edgar_document_max_bytes),
    so a routine filing like this one is never truncated."""
    body = _fixture_bytes("AAPL_10K_0000320193-25-000079.htm")
    assert len(body) == 1_520_208
    t = ScriptedTransport()
    t.enqueue_raw(200, body)
    client, _ = edgar_client(t)
    result = client.filing_document("320193", "0000320193-25-000079",
                                     "aapl-20250930.htm",
                                     max_bytes=5_000_000)
    assert result.truncated is False
    assert result.byte_length == 1_520_208
    assert result.sha256 == hashlib.sha256(body).hexdigest()
