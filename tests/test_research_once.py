"""agent/research_once.py (Task 3, Phase-2/3-live-acceptance follow-up unit,
2026-08-15) -- the safe out-of-session `--research-once` command. No test
here makes a network call: every `EdgarClient`/`AlpacaMarketDataClient` is
bound to a real `ScriptedTransport` (same discipline as tests/
test_edgar_collector.py/tests/test_market_data_collector.py), and news uses
`agent.news_provider.InMemoryNewsProvider`/`NullNewsProvider` -- the
established test-only, network-free collaborators for each real collector.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from agent.accounts import BrokerCredentials
from agent.broker.alpaca_market_data import AlpacaMarketDataClient
from agent.broker.transport import ScriptedTransport
from agent.cost import CostLedger
from agent.edgar import EdgarClient
from agent.edgar_collector import TickerCikCache
from agent.materiality import DEFAULT_FILING_WEIGHTS, MaterialityPolicy
from agent.mode_store import ModeStore
from agent.news_provider import InMemoryNewsProvider, NewsEvent, NullNewsProvider
from agent.opportunity_event_store import OpportunityEventStore
from agent.policy import initial_policy
from agent.research_once import ResearchOnceRefused, run_research_once
from agent.secrets_provider import InMemorySecretsProvider
from agent.store import FactStore

# A real trading Monday (matches every other test file's own T0).
T0 = datetime(2026, 7, 20, 15, 0, tzinfo=timezone.utc)
SATURDAY = datetime(2026, 7, 18, 15, 0, tzinfo=timezone.utc)
UNIVERSE = {"AAPL": "US_EQUITY"}
POLICY = MaterialityPolicy(version="mat-v1", w1=1.0, w2=1.0, w3=1.0, w4=0.0,
                          w5=0.0, w6=1.0, threshold=2.0,
                          filing_weights=DEFAULT_FILING_WEIGHTS)
ACCT = "acct-a"
UA = "InvestmentAgent Pilot test@example.com"


def _mode_store(tmp_path, mode="PAUSED"):
    store = ModeStore(tmp_path / "mode_state.jsonl")
    if mode != "DISABLED":
        # DISABLED is ModeStore's own bootstrap default -- everything else
        # needs an explicit write.
        store.write(mode, changed_at=T0)
    return store


def _cost_ledger():
    return CostLedger(monthly_budget=20.0, warning_at=15.0, hard_stop_at=30.0)


def _edgar_client(transport=None):
    return EdgarClient(user_agent=UA, transport=transport or ScriptedTransport(),
                       http_timeout_seconds=1.0, http_max_retries=1,
                       min_request_interval_seconds=0.001,
                       sleep_fn=lambda s: None, monotonic_fn=lambda: 0.0)


def _market_data_client(transport=None):
    secrets = InMemorySecretsProvider(mode="PAPER")
    secrets.put("alpaca-secret", "s3cr3t")
    return AlpacaMarketDataClient(
        credentials=BrokerCredentials(account_id=ACCT, key_id="AK1",
                                      secret_ref="alpaca-secret"),
        secrets_provider=secrets, feed="iex", transport=transport or ScriptedTransport(),
        http_timeout_seconds=1.0, http_max_retries=1,
    )


def ticker_map_body():
    return {"0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."}}


def submissions_body_empty():
    return {"filings": {"recent": {
        "form": [], "filingDate": [], "reportDate": [], "acceptanceDateTime": [],
        "accessionNumber": [], "primaryDocument": [], "items": [],
    }, "files": []}}


def _run(*, tmp_path, mode="PAUSED", now=T0, edgar_client=None, market_data_client=None,
         news_provider=None, fact_store=None, opportunity_event_store=None):
    mode_store = _mode_store(tmp_path, mode=mode)
    return run_research_once(
        mode_store=mode_store,
        # NOTE: `fact_store if fact_store is not None else ...`, deliberately
        # NOT `fact_store or ...` -- an empty-but-real FactStore/
        # OpportunityEventStore is falsy (both define __len__, and Python
        # falls back to it for truthiness), so `or` would silently swap in
        # a FRESH store instead of the caller's own empty one, right when a
        # test most needs to assert against that exact object afterward.
        fact_store=fact_store if fact_store is not None else FactStore(),
        opportunity_event_store=(
            opportunity_event_store if opportunity_event_store is not None
            else OpportunityEventStore(tmp_path / "materiality_events.jsonl")),
        symbol_universe=UNIVERSE, materiality_policy=POLICY,
        capability_policy=initial_policy(), cost_ledger=_cost_ledger(),
        max_model_analyses_per_day=8, max_approval_requests_per_day=4,
        min_peer_group_size=1,
        market_data_client=market_data_client, edgar_client=edgar_client or _edgar_client(),
        ticker_cik_cache=TickerCikCache(), ticker_cik_refresh_max_age=timedelta(hours=24),
        news_provider=news_provider or NullNewsProvider(),
        news_lookback=timedelta(hours=24), now=now,
    )


# --------------------------------------------------------- PAUSED precondition

def test_refuses_when_persisted_mode_is_not_paused(tmp_path):
    with pytest.raises(ResearchOnceRefused, match="PAUSED"):
        _run(tmp_path=tmp_path, mode="PAPER")


@pytest.mark.parametrize("mode", ["DISABLED", "RESEARCH", "PAPER", "PRODUCTION_ACTIVE"])
def test_refuses_for_every_non_paused_mode(tmp_path, mode):
    with pytest.raises(ResearchOnceRefused):
        _run(tmp_path=tmp_path, mode=mode)


def test_a_refusal_touches_no_store_at_all(tmp_path):
    """No collection, no screening, no persistence attempted when the
    precondition fails -- the refusal happens BEFORE any collaborator is
    touched."""
    opp_store = OpportunityEventStore(tmp_path / "materiality_events.jsonl")
    fact_store = FactStore(tmp_path / "facts.jsonl")
    with pytest.raises(ResearchOnceRefused):
        _run(tmp_path=tmp_path, mode="PAPER", fact_store=fact_store,
            opportunity_event_store=opp_store)
    assert len(fact_store) == 0
    assert len(opp_store) == 0


def test_succeeds_while_paused(tmp_path):
    result = _run(tmp_path=tmp_path, mode="PAUSED")
    assert result.persisted_mode == "PAUSED"


def test_mode_store_write_is_never_called_structurally():
    """Static proof, mirroring _run_reconcile_once's own CANNOT REACH AN
    ORDER section: agent.research_once never calls ModeStore.write at all."""
    import ast
    from pathlib import Path
    import agent.research_once as ro_module
    source = Path(ro_module.__file__).read_text()
    tree = ast.parse(source, ro_module.__file__)
    called_attrs = {n.func.attr for n in ast.walk(tree)
                   if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    assert "write" not in called_attrs


def test_module_never_imports_pipeline_or_approval_or_model_machinery():
    """Static proof: no Gatekeeper, no StagedOrder, no approval execution,
    no T4/Claude anywhere in this module's own imports."""
    import ast
    from pathlib import Path
    import agent.research_once as ro_module
    source = Path(ro_module.__file__).read_text()
    tree = ast.parse(source, ro_module.__file__)
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
        elif isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
    forbidden = ("agent.pipeline", "agent.approval", "agent.analysis_trigger",
                "agent.model_client", "agent.broker.alpaca ", "agent.broker.base")
    joined = " ".join(names)
    assert "pipeline" not in joined or "pipeline_stage" not in joined  # sanity: exercised below
    for fragment in ("pipeline", "approval", "analysis_trigger", "model_client"):
        assert not any(fragment in n for n in names), f"found forbidden import fragment: {fragment}"
    assert not any(n.endswith(".alpaca") for n in names)   # AlpacaPaperAdapter's own module


def test_module_never_calls_submit_or_cancel():
    import ast
    from pathlib import Path
    import agent.research_once as ro_module
    source = Path(ro_module.__file__).read_text()
    tree = ast.parse(source, ro_module.__file__)
    called = {n.func.attr for n in ast.walk(tree)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    assert "submit" not in called
    assert "cancel" not in called


# ------------------------------------------------------- market data provider

def test_market_data_is_not_yet_observed_when_no_client_given(tmp_path):
    result = _run(tmp_path=tmp_path, market_data_client=None)
    assert result.market_data.status == "NOT_YET_OBSERVED"
    assert result.market_data.facts_collected == 0
    assert "no market data client" in result.market_data.reason


def test_market_data_is_not_yet_observed_on_a_non_trading_day(tmp_path):
    client = _market_data_client()
    result = _run(tmp_path=tmp_path, now=SATURDAY, market_data_client=client)
    assert result.market_data.status == "NOT_YET_OBSERVED"
    assert "not a trading day" in result.market_data.reason
    # No network call was even attempted -- the ScriptedTransport was never
    # given anything to serve, so a stray real call would raise (empty
    # queue), not silently succeed. This assertion holds by construction of
    # the fixture below (nothing enqueued), reinforced by the reason text.


def test_market_data_is_not_yet_observed_before_todays_session_open(tmp_path):
    before_open = T0.replace(hour=12)   # NYSE opens 13:30 UTC in summer
    client = _market_data_client()
    result = _run(tmp_path=tmp_path, now=before_open, market_data_client=client)
    assert result.market_data.status == "NOT_YET_OBSERVED"
    assert "before today" in result.market_data.reason


def test_market_data_collects_when_in_session_with_real_bars(tmp_path):
    """A real, in-session collection -- proves the command DOES honor "if
    the market provider can truthfully retrieve the most recent completed
    bar/quote, that is acceptable" rather than refusing collection
    unconditionally just because this is the --research-once path."""
    transport = ScriptedTransport()
    today_open = T0.replace(hour=13, minute=30)
    # 21 daily bars (>= _ATR_LOOKBACK + 1) then minute bars covering today.
    daily_bars = [
        {"t": (today_open - timedelta(days=21 - i)).strftime("%Y-%m-%dT%H:%M:%SZ"),
         "o": 100.0, "h": 101.0, "l": 99.0, "c": 100.0, "v": 1000, "n": 1, "vw": 100.0}
        for i in range(21)
    ]
    transport.enqueue(200, {"bars": {"AAPL": daily_bars}, "next_page_token": None})
    minute_bars = [
        {"t": (today_open + timedelta(minutes=m)).strftime("%Y-%m-%dT%H:%M:%SZ"),
         "o": 100.0, "h": 101.0, "l": 99.0, "c": 100.0 + m * 0.01, "v": 100, "n": 1, "vw": 100.0}
        for m in range(5)
    ]
    transport.enqueue(200, {"bars": {"AAPL": minute_bars}, "next_page_token": None})
    client = _market_data_client(transport)
    result = _run(tmp_path=tmp_path, now=today_open + timedelta(minutes=5),
                 market_data_client=client)
    # Not enough real historical sessions in this fixture for a full
    # same-time-window comparison -- the point of this test is that the
    # collector actually RAN (COLLECTED, not silently skipped for being
    # --research-once), not that every symbol produced a snapshot.
    assert result.market_data.status == "COLLECTED"


# -------------------------------------------------------------- EDGAR filings

def test_edgar_filings_collect_out_of_session_on_a_non_trading_day(tmp_path):
    """THE MISSION'S OWN POINT: EDGAR research can run any time, including
    a Saturday when the market is definitely closed."""
    transport = ScriptedTransport()
    transport.enqueue(200, ticker_map_body())
    accepted = "2026-07-17T16:30:41.000Z"
    transport.enqueue(200, {"filings": {"recent": {
        "form": ["8-K"], "filingDate": ["2026-07-17"], "reportDate": ["2026-07-17"],
        "acceptanceDateTime": [accepted], "accessionNumber": ["0001"],
        "primaryDocument": ["doc.htm"], "items": ["2.02"],
    }, "files": []}})
    client = _edgar_client(transport)
    fact_store = FactStore(tmp_path / "facts.jsonl")
    result = _run(tmp_path=tmp_path, now=SATURDAY, edgar_client=client,
                 fact_store=fact_store)
    assert result.edgar_filings.status == "COLLECTED"
    assert result.edgar_filings.facts_collected == 1
    assert len(fact_store) == 1


def test_edgar_filings_reports_real_dedup_count(tmp_path):
    """A second run against the SAME already-known filing must report it as
    a real, honest duplicate, not silently absorb it into facts_collected."""
    fact_store = FactStore(tmp_path / "facts.jsonl")
    transport1 = ScriptedTransport()
    transport1.enqueue(200, ticker_map_body())
    accepted = "2026-07-17T16:30:41.000Z"
    filing_body = {"filings": {"recent": {
        "form": ["8-K"], "filingDate": ["2026-07-17"], "reportDate": ["2026-07-17"],
        "acceptanceDateTime": [accepted], "accessionNumber": ["0001"],
        "primaryDocument": ["doc.htm"], "items": ["2.02"],
    }, "files": []}}
    transport1.enqueue(200, filing_body)
    _run(tmp_path=tmp_path, now=SATURDAY, edgar_client=_edgar_client(transport1),
        fact_store=fact_store)

    transport2 = ScriptedTransport()
    transport2.enqueue(200, ticker_map_body())
    transport2.enqueue(200, filing_body)
    result2 = _run(tmp_path=tmp_path, now=SATURDAY, edgar_client=_edgar_client(transport2),
                   fact_store=fact_store)
    assert result2.edgar_filings.status == "COLLECTED"
    assert result2.edgar_filings.facts_collected == 0
    assert result2.edgar_filings.facts_deduplicated == 1


def test_edgar_client_failure_is_not_yet_observed_not_a_crash(tmp_path):
    """An empty ScriptedTransport queue (simulating a real network failure
    outside this sandbox) is caught and reported, never propagated."""
    client = _edgar_client(ScriptedTransport())   # nothing enqueued
    result = _run(tmp_path=tmp_path, now=SATURDAY, edgar_client=client)
    assert result.edgar_filings.status == "NOT_YET_OBSERVED"
    assert result.edgar_filings.reason


# ------------------------------------------------------------------ news

def test_news_collects_out_of_session(tmp_path):
    provider = InMemoryNewsProvider([
        NewsEvent(symbol="AAPL", headline="A real headline", url="https://example.com/1",
                 provider_name="test_provider", published_at=SATURDAY - timedelta(hours=1)),
    ])
    fact_store = FactStore(tmp_path / "facts.jsonl")
    result = _run(tmp_path=tmp_path, now=SATURDAY, news_provider=provider,
                 fact_store=fact_store)
    assert result.news.status == "COLLECTED"
    assert result.news.facts_collected == 1


def test_news_reports_real_dedup_count(tmp_path):
    provider = InMemoryNewsProvider([
        NewsEvent(symbol="AAPL", headline="A real headline", url="https://example.com/1",
                 provider_name="test_provider", published_at=SATURDAY - timedelta(hours=1)),
    ])
    fact_store = FactStore(tmp_path / "facts.jsonl")
    _run(tmp_path=tmp_path, now=SATURDAY, news_provider=provider, fact_store=fact_store)
    result2 = _run(tmp_path=tmp_path, now=SATURDAY, news_provider=provider,
                   fact_store=fact_store)
    assert result2.news.facts_collected == 0
    assert result2.news.facts_deduplicated == 1


def test_no_news_provider_is_not_yet_observed(tmp_path):
    result = _run(tmp_path=tmp_path, news_provider=None) if False else None
    # NullNewsProvider is the harness's own default -- exercised by every
    # other test above via `_run`'s own default; this test proves the
    # `provider is None` branch specifically (never actually reachable from
    # scripts/run_agent.py, which always constructs a real provider via
    # agent.config.build_provider -- but agent.research_once.run_research_
    # once accepts None as a defensive, honestly-reported case, same
    # posture as market_data_client=None).
    from agent.research_once import run_research_once as _rro
    mode_store = _mode_store(tmp_path)
    result = _rro(
        mode_store=mode_store, fact_store=FactStore(),
        opportunity_event_store=OpportunityEventStore(tmp_path / "materiality_events.jsonl"),
        symbol_universe=UNIVERSE, materiality_policy=POLICY,
        capability_policy=initial_policy(), cost_ledger=_cost_ledger(),
        max_model_analyses_per_day=8, max_approval_requests_per_day=4,
        min_peer_group_size=1, market_data_client=None, edgar_client=_edgar_client(),
        ticker_cik_cache=TickerCikCache(), ticker_cik_refresh_max_age=timedelta(hours=24),
        news_provider=None, news_lookback=timedelta(hours=24), now=SATURDAY,
    )
    assert result.news.status == "NOT_YET_OBSERVED"
    assert "no news provider" in result.news.reason


# ------------------------------------------------------- materiality screening

def test_screening_runs_and_persists_a_real_event_out_of_session(tmp_path):
    """The mission's own headline goal: real materiality screening, from
    real persisted facts, durably persisted -- while genuinely PAUSED, on a
    day the market is closed."""
    fact_store = FactStore(tmp_path / "facts.jsonl")
    from agent.market_data_collector import FIELD as SNAPSHOT_FIELD, SOURCE_ID as MKT_SOURCE
    from agent.edgar_collector import FIELD as FILING_FIELD, SOURCE_ID as EDGAR_SOURCE
    from agent.store import Fact
    fact_store.append(Fact(
        entity_id="AAPL", field=SNAPSHOT_FIELD,
        value={"atr_20": 0.1, "ret_since_open": 5.0, "volume_so_far": 100.0,
              "median_volume_same_time": 100.0, "current_price": 200.0},
        observed_at=SATURDAY, effective_at=SATURDAY, source_id=MKT_SOURCE,
    ))
    fact_store.append(Fact(
        entity_id="AAPL", field=FILING_FIELD,
        value={"cik": "0000000001", "form": "8-K", "item_codes": ["2.02"],
              "accession_number": "0001", "primary_document": "doc.htm",
              "filing_date": SATURDAY.date().isoformat(),
              "report_date": SATURDAY.date().isoformat()},
        observed_at=SATURDAY, effective_at=SATURDAY, source_id=EDGAR_SOURCE,
        source_doc_hash="0001",
    ))
    opp_store = OpportunityEventStore(tmp_path / "materiality_events.jsonl")
    result = _run(tmp_path=tmp_path, now=SATURDAY, fact_store=fact_store,
                 opportunity_event_store=opp_store)
    assert result.materiality_evaluations >= 1
    assert result.events_persisted == result.materiality_evaluations
    assert result.events_persistence_failed == 0
    assert len(opp_store) == result.materiality_evaluations
    assert result.triggered + result.suppressed + result.not_material == result.materiality_evaluations


def test_report_counts_are_internally_consistent_with_zero_facts(tmp_path):
    result = _run(tmp_path=tmp_path, now=SATURDAY)
    assert result.materiality_evaluations == 0
    assert result.triggered == 0
    assert result.suppressed == 0
    assert result.not_material == 0
    assert result.events_persisted == 0
    assert result.events_persistence_failed == 0


def test_held_and_cooldown_awareness_is_disclosed_in_the_report(tmp_path):
    result = _run(tmp_path=tmp_path, now=SATURDAY)
    assert "side=\"BUY\"" in result.held_and_cooldown_awareness
    assert "cooldown" in result.held_and_cooldown_awareness


def test_no_future_leakage_a_fact_observed_after_now_is_never_screened(tmp_path):
    """AsOfView's own no-lookahead invariant, exercised end-to-end through
    this command: a fact observed AFTER `now` must never be visible to the
    screen this run performs."""
    fact_store = FactStore(tmp_path / "facts.jsonl")
    from agent.market_data_collector import FIELD as SNAPSHOT_FIELD, SOURCE_ID as MKT_SOURCE
    from agent.store import Fact
    future = SATURDAY + timedelta(days=1)
    fact_store.append(Fact(
        entity_id="AAPL", field=SNAPSHOT_FIELD,
        value={"atr_20": 0.1, "ret_since_open": 5.0, "volume_so_far": 100.0,
              "median_volume_same_time": 100.0, "current_price": 200.0},
        observed_at=future, effective_at=future, source_id=MKT_SOURCE,
    ))
    result = _run(tmp_path=tmp_path, now=SATURDAY, fact_store=fact_store)
    assert result.materiality_evaluations == 0   # the future fact was invisible


def test_collected_now_is_distinct_from_each_facts_own_effective_at(tmp_path):
    """The command's own `now` (collected_now) is never written back onto a
    collected Fact's own observed_at/effective_at -- each collector's own
    real timestamp is preserved verbatim."""
    transport = ScriptedTransport()
    transport.enqueue(200, ticker_map_body())
    accepted = "2026-07-15T16:30:41.000Z"   # two days before `now` below
    transport.enqueue(200, {"filings": {"recent": {
        "form": ["8-K"], "filingDate": ["2026-07-15"], "reportDate": ["2026-07-15"],
        "acceptanceDateTime": [accepted], "accessionNumber": ["0001"],
        "primaryDocument": ["doc.htm"], "items": ["2.02"],
    }, "files": []}})
    fact_store = FactStore(tmp_path / "facts.jsonl")
    _run(tmp_path=tmp_path, now=SATURDAY, edgar_client=_edgar_client(transport),
        fact_store=fact_store)
    facts = fact_store.all_facts()
    assert len(facts) == 1
    assert facts[0].observed_at != SATURDAY
    assert facts[0].observed_at.date().isoformat() == "2026-07-15"
