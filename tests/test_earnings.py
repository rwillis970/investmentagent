"""agent/earnings.py (§3.2 `earnings_proximity(t)`, Commit 3).

No forward-looking earnings-calendar source exists for free (see module
docstring for the research trail) -- this tests only the BACKWARD-derived,
estimated-cadence half: a symbol's own past 8-K item 2.02 filings (already
collected by Commit 2's `agent.edgar_collector`) project an ESTIMATED next
earnings date, and proximity decays around that estimate. `None` (not
`0.0`) whenever fewer than two prior releases are on record -- there is no
cadence to estimate from, and `0.0` would falsely assert "not proximate"
rather than admit the input is unknown.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

from agent.earnings import (EARNINGS_RELEASE_ITEM, compute_earnings_proximity,
                            earnings_proximity, earnings_release_dates)
from agent.edgar_collector import FIELD as FILING_FIELD
from agent.edgar_collector import SOURCE_ID as EDGAR_SOURCE_ID
from agent.store import Fact, FactStore

T0 = datetime(2026, 7, 31, 15, 0, tzinfo=timezone.utc)


def filing_fact(*, symbol="ACME", form="8-K", item_codes=("2.02",),
                effective_at, observed_at=T0, accession="0001"):
    return Fact(
        entity_id=symbol, field=FILING_FIELD,
        value={"cik": "0000000001", "form": form, "item_codes": list(item_codes),
              "accession_number": accession, "primary_document": "doc.htm",
              "filing_date": effective_at.date().isoformat(),
              "report_date": effective_at.date().isoformat()},
        observed_at=observed_at, effective_at=effective_at,
        source_id=EDGAR_SOURCE_ID, source_doc_hash=accession,
    )


# ------------------------------------------------------- earnings_release_dates

def test_earnings_release_dates_finds_8k_filings_carrying_item_2_02():
    store = FactStore()
    store.append(filing_fact(effective_at=datetime(2026, 1, 30, tzinfo=timezone.utc),
                             accession="0001"))
    view = store.now_view()
    assert earnings_release_dates(view, "ACME") == [date(2026, 1, 30)]


def test_earnings_release_dates_ignores_8k_without_item_2_02():
    store = FactStore()
    store.append(filing_fact(effective_at=datetime(2026, 1, 30, tzinfo=timezone.utc),
                             item_codes=("5.02",), accession="0001"))
    view = store.now_view()
    assert earnings_release_dates(view, "ACME") == []


def test_earnings_release_dates_ignores_non_8k_forms():
    store = FactStore()
    store.append(filing_fact(effective_at=datetime(2026, 1, 30, tzinfo=timezone.utc),
                             form="10-Q", item_codes=(), accession="0001"))
    view = store.now_view()
    assert earnings_release_dates(view, "ACME") == []


def test_earnings_release_dates_are_sorted_oldest_first_and_deduplicated():
    store = FactStore()
    store.append(filing_fact(effective_at=datetime(2026, 4, 30, tzinfo=timezone.utc),
                             accession="0002"))
    store.append(filing_fact(effective_at=datetime(2026, 1, 30, tzinfo=timezone.utc),
                             accession="0001"))
    store.append(filing_fact(effective_at=datetime(2026, 1, 30, tzinfo=timezone.utc),
                             accession="0003"))   # same date, different accession
    view = store.now_view()
    assert earnings_release_dates(view, "ACME") == [date(2026, 1, 30), date(2026, 4, 30)]


def test_earnings_release_dates_respects_the_look_ahead_guard():
    store = FactStore()
    fact = filing_fact(effective_at=datetime(2026, 1, 30, tzinfo=timezone.utc),
                       observed_at=datetime(2026, 1, 30, 20, 0, tzinfo=timezone.utc),
                       accession="0001")
    store.append(fact)
    before = store.as_of(datetime(2026, 1, 30, 19, 0, tzinfo=timezone.utc))
    after = store.as_of(datetime(2026, 1, 30, 21, 0, tzinfo=timezone.utc))
    assert earnings_release_dates(before, "ACME") == []
    assert earnings_release_dates(after, "ACME") == [date(2026, 1, 30)]


def test_earnings_release_dates_scoped_to_the_given_symbol():
    store = FactStore()
    store.append(filing_fact(symbol="ACME", effective_at=datetime(2026, 1, 30, tzinfo=timezone.utc),
                             accession="0001"))
    store.append(filing_fact(symbol="OTHR", effective_at=datetime(2026, 2, 1, tzinfo=timezone.utc),
                             accession="0002"))
    view = store.now_view()
    assert earnings_release_dates(view, "ACME") == [date(2026, 1, 30)]


# ------------------------------------------------------ compute_earnings_proximity

def test_fewer_than_two_releases_is_none_not_zero():
    """Insufficient history to derive a cadence is UNKNOWN, not 'no earnings
    nearby' -- a hardcoded 0.0 here would be a claim this module cannot back."""
    assert compute_earnings_proximity([], t=date(2026, 7, 31)) is None
    assert compute_earnings_proximity([date(2026, 1, 30)], t=date(2026, 7, 31)) is None


def test_two_releases_project_an_estimated_next_date_from_the_median_interval():
    # Exactly 91 days apart -> estimated next release is 91 days after the
    # second, i.e. 2026-07-30. t is exactly on the estimate -> proximity 1.0.
    releases = [date(2026, 1, 29), date(2026, 4, 30)]
    proximity = compute_earnings_proximity(releases, t=date(2026, 7, 30))
    assert proximity == 1.0


def test_proximity_decays_linearly_away_from_the_estimated_date():
    releases = [date(2026, 1, 29), date(2026, 4, 30)]   # estimate: 2026-07-30
    at_estimate = compute_earnings_proximity(releases, t=date(2026, 7, 30))
    two_days_off = compute_earnings_proximity(releases, t=date(2026, 8, 1))
    far_off = compute_earnings_proximity(releases, t=date(2026, 10, 1))
    assert at_estimate == 1.0
    assert 0.0 < two_days_off < at_estimate
    assert far_off == 0.0


def test_proximity_is_symmetric_around_the_estimated_date():
    releases = [date(2026, 1, 29), date(2026, 4, 30)]   # estimate: 2026-07-30
    before = compute_earnings_proximity(releases, t=date(2026, 7, 28))
    after = compute_earnings_proximity(releases, t=date(2026, 8, 1))
    assert before == after


def test_proximity_uses_the_median_interval_across_more_than_two_releases():
    # Intervals: 90, 92, 91 days -> median 91 -> estimate = last + 91
    releases = [date(2026, 1, 1), date(2026, 4, 1), date(2026, 7, 2), date(2026, 10, 1)]
    estimated_next = date(2026, 10, 1)
    # (91 days later)
    from datetime import timedelta
    estimated_next = releases[-1] + timedelta(days=91)
    assert compute_earnings_proximity(releases, t=estimated_next) == 1.0


# ----------------------------------------------------------------- earnings_proximity

def test_earnings_proximity_end_to_end_from_stored_filings():
    store = FactStore()
    store.append(filing_fact(effective_at=datetime(2026, 1, 29, tzinfo=timezone.utc), accession="0001"))
    store.append(filing_fact(effective_at=datetime(2026, 4, 30, tzinfo=timezone.utc), accession="0002"))
    view = store.now_view()
    assert earnings_proximity(view, "ACME", t=date(2026, 7, 30)) == 1.0


def test_earnings_proximity_is_none_with_only_one_stored_release():
    store = FactStore()
    store.append(filing_fact(effective_at=datetime(2026, 1, 29, tzinfo=timezone.utc), accession="0001"))
    view = store.now_view()
    assert earnings_proximity(view, "ACME", t=date(2026, 7, 30)) is None


def test_earnings_proximity_is_none_for_a_symbol_with_no_filings_at_all():
    store = FactStore()
    view = store.now_view()
    assert earnings_proximity(view, "ACME", t=date(2026, 7, 30)) is None


def test_earnings_release_item_constant_matches_sec_results_of_operations_code():
    assert EARNINGS_RELEASE_ITEM == "2.02"
