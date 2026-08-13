# After-hours collection architecture (review)

**Status: design/finding only. No behavior changed by this document.**
Overnight-hardening unit, 2026-08-13, item 6 of 8. Referenced by name from
`agent/diagnostics.py`'s own module docstring ("a separate, not-yet-wired
half of 'safe outside a session'"); this file is that reference resolved.

This unit's own instructions were explicit: *"Do not turn this into a huge
new feature tonight... Document the current behavior. If a small clean
refactor can allow ingestion/screening outside the order-execution session
without weakening trading controls, implement it. Otherwise produce a
concrete design and leave implementation for a separate unit... Do NOT add
a news provider in this unit."* Having traced the actual call graph (below),
the refactor that would be required is **not** small or low-risk enough to
do in the same pass as tonight's other changes, for reasons given in
"Why this is not a small refactor" below. This document is the concrete
design; implementation is deliberately left for a separate, dedicated unit.

## 1. Current behavior, exactly as coded today

`scripts/run_agent.py`'s real entry point calls `agent.run_loop.run_loop`,
whose body is:

```python
while max_cycles is None or cycles_run < max_cycles:
    now = now_fn()
    if in_session_now(now):
        report = run_cycle(...)          # reconciliation AND the pipeline stage
        ...
    else:
        sleep_for = seconds_until_next_session_open(now)
        ...
    sleep_fn(sleep_for)
```

`in_session_now(now)` (`agent/run_loop.py`) is a single boolean: is `now`
strictly within a real NYSE regular session (`agent.market_calendar`). It is
the ONLY gate. When it is `False` — nights, weekends, holidays, the entire
non-session part of every day — `run_cycle` is not called **at all**. Not a
reduced version of it; none of it.

`run_cycle` does two structurally different things, and today's single gate
does not distinguish between them:

1. **Reconciliation** (`sync_fills`, `close_terminal_orders`,
   `sync_cash_events`, `build_account_reconciliation`, `run_startup`) — all
   read-only against the broker (`account`, `positions`, `open_orders`,
   `fills` — never `submit`/`cancel`, confirmed directly by `agent.
   diagnostics`'s own AST-based import/call-tracking tests, which exercise
   the identical read-only adapter surface `run_cycle` uses). This
   genuinely needs `in_session_now` in the sense that "does the broker's
   picture of today's activity match ours" is a question that only has a
   stable answer once a session has actually had trading in it — but even
   this is a convenience gate, not a hard requirement: nothing about
   `sync_fills`/`build_account_reconciliation` would be UNSAFE to run at
   11pm, it would just usually find nothing new to reconcile (the broker's
   own state does not change overnight either). This is exactly the gap
   `agent.diagnostics`/`scripts/diagnose_runtime.py` (items 1 and 2 of this
   same overnight unit) already closed for the READ-ONLY HEALTH-CHECK use
   case, deliberately without touching this loop.

2. **The pipeline stage** (`agent.pipeline_stage.run_pipeline_stage`,
   called only after `run_startup` succeeds) — collection, screening, T4
   analysis, and approval-request creation. Only the FIRST of these four is
   genuinely read-only evidence-gathering with no decision attached
   (`agent.pipeline_stage`'s own module docstring: "Collection is NOT
   mode-gated (pure evidence gathering, no decision..."). The other three
   form opinions, spend money, or present a decision to the operator.

`run_pipeline_stage` itself ALSO carries an internal, redundant session
check on just the collection block:

```python
if (pipeline.data_collection_enabled and _in_session_now(now)
        and _due(last_collected_at, pipeline.data_collection_interval_seconds, now)):
    collect_market_data(...); collect_filings(...); collect_news_events(...)
```

Because `run_pipeline_stage` is never reached at all when `run_loop`'s outer
gate is closed, this inner check is currently dead weight for the "is it
after hours" question — it only ever evaluates `True` inside an outer
`in_session_now(now) == True` block already. Screening, T4, and
approval-request creation have **no session check of their own at all** —
they are gated only by their own feature flag, cadence, and
`mode not in ("DISABLED", "PAUSED")`. Today that is safe only because they
are unreachable outside a session by construction (the outer gate). It is
NOT safe on its own if the outer gate were simply removed or loosened — see
next section.

### A related, independently-discovered finding: order SUBMISSION has no session gate at all

While tracing this, one adjacent fact is worth recording even though it is
outside this item's own scope (collection, not execution) and is NOT
touched by this unit: `scripts/run_agent.py --submit-approved` (the CLI
flag that calls `agent.approval_execution`'s real submission path) does not
call `in_session_now` anywhere in its own code path — neither
`agent/approval_execution.py` nor its callers reference `agent.run_loop` or
`agent.market_calendar` at all. An operator invoking `--submit-approved`
at 9pm today would have that call proceed exactly as it would at 10am; the
only backstop is whatever Alpaca's own API does with an order placed
outside its accepted trading window (this codebase does not currently
inspect or rely on any specific broker-side response to that). This is a
genuine, disclosed gap in the execution path, not the collection path this
item was asked to review — flagged here for the final report's own "new
defects discovered" section (item 11), not fixed in this unit (fixing it
means changing `agent/approval_execution.py`, an execution-path module, and
this unit's own instructions are collection/diagnostics/deployment only).

## 2. Why this is not a small refactor

Removing or loosening `run_loop`'s single outer `in_session_now` gate to
let `run_cycle` (or a new function reusing `run_pipeline_stage`) run after
hours has a real, non-cosmetic consequence: **screening, T4 analysis, and
approval-request creation would run after hours too**, because none of them
has its own independent session gate — only the outer loop gate stops them
today. That is a materially different, larger change than "let collection
run at night":

- **T4 analysis spends real money** (`t4_analysis_enabled`, the flag this
  unit's own instructions explicitly say not to touch: "DO NOT enable T4
  analysis"). A refactor that accidentally makes T4 reachable after hours,
  even if the flag itself stays `false` in every real deployment tonight,
  changes what "the flag is off" is the only thing standing between the
  system and an after-hours model call — today two independent facts stand
  between them (the flag AND the session gate); after a naive refactor,
  only the flag would.
- **Approval-request creation prices a decision against an after-hours
  quote.** `agent.approval_trigger.request_approval_for_analysis` reads
  `current_price` from whatever the fact store's most recent market-data
  snapshot is (`read_market_snapshot`) and binds it into the approval
  token's own price band (`price_band_pct`). Alpaca's data API does return
  after-hours/extended-session quotes, but this codebase has never
  evaluated whether a price band computed from a thin after-hours quote is
  the right thing to present to an operator, or whether `agent.approval_
  execution`'s price-band check at submission time (itself running with NO
  session gate of its own — see finding above) should treat an after-hours-
  priced token differently. That evaluation has not been done and is not
  small.
- **`materiality_screen_enabled` is one flag for the whole screening
  block**, not "screen but don't trigger T4" and "trigger T4 but don't
  request approval" as separately addressable knobs. Decoupling "collect
  after hours" from "screen/analyze/request after hours" cleanly requires
  either a new, additional gate INSIDE `run_pipeline_stage` (touching the
  one shared pipeline-stage function this codebase deliberately keeps
  single, per this project's own "ONE code path" discipline) or a second,
  parallel entry point that duplicates parts of it — both are real design
  decisions, not one-line changes, and both touch `agent/pipeline_stage.py`,
  which is exercised by a large, interlocking existing test suite (Units
  1-5, the earmarking unit, and the bridge unit all added tests against
  this exact module).

Given the instruction to avoid turning this into "a huge new feature
tonight," and given that this file exists to document + design rather than
implement, the correct call is: document precisely (above), propose a
concrete, narrow design (below), and leave implementation to a dedicated
follow-up unit that can give the price-band/after-hours-quote question the
attention it needs on its own, with its own tests-first pass.

## 3. Proposed design for a follow-up unit

**Principle: collection is the only stage that is genuinely safe to
decouple from the session gate, and it should be decoupled via a NEW,
separate, narrower entry point — not by loosening the existing one.**

1. Extract the three collector calls (`collect_market_data`,
   `collect_filings`, `collect_news_events`) out of `run_pipeline_stage`'s
   inline block into their own function, e.g. `agent.pipeline_stage.
   run_collection_only(pipeline, *, now) -> datetime | None` (returns the
   new `last_collected_at` or `None` if not due/not enabled — same
   contract the inline block has today, just named and callable on its
   own). `run_pipeline_stage` keeps calling it internally so behavior
   during a real session is provably unchanged (this refactor step alone
   is safe, mechanical, and could be done as a small commit with a
   golden-output test asserting identical `FactStore` writes before/after).

2. `run_collection_only` takes **no broker adapter, no `Ledger`, no
   `mode_store`, no `audit_log`** — collection genuinely does not need any
   of them today (verified above: `collect_market_data`/`collect_filings`/
   `collect_news_events` take only `market_data_client`/`edgar_client`/
   `news_provider`/`FactStore`/`symbols`/`now`). This means an after-hours
   collection cycle would not need to resolve any broker credential and
   would not need a keychain unlocked — strictly LOWER-risk than the
   existing reconciliation loop it runs alongside during a session, not
   higher.

3. `agent.run_loop.run_loop` grows a second, independent cadence,
   symmetrical to how it already tracks `last_collected_at`/
   `last_screened_at` across iterations, e.g.:

   ```python
   if in_session_now(now):
       report = run_cycle(...)                      # unchanged
   elif pipeline is not None and pipeline.after_hours_collection_enabled:
       last_collected_at = run_collection_only(pipeline, now=now)
       sleep_for = <collection cadence, independent of session>
   else:
       sleep_for = seconds_until_next_session_open(now)   # unchanged default
   ```

   `after_hours_collection_enabled` is a NEW, independently-defaulting-to-
   `False` flag (`agent.config.Config`), following the exact convention
   every other pipeline stage flag already uses (`data_collection_enabled`,
   `materiality_screen_enabled`, `t4_analysis_enabled`,
   `approval_request_enabled` — see `agent/pipeline_stage.py`'s own "MONEY
   GUARDRAIL" section). A fresh checkout/restart makes zero new after-hours
   collector calls until an operator opts in explicitly, exactly like every
   other stage.

4. **Screening, T4, and approval-request creation are NOT included in this
   follow-up unit's scope either** — `run_collection_only` never calls
   `run_materiality_cycle`/`analyze_opportunity_event`/
   `request_approval_for_analysis`. The follow-up unit's own final report
   should treat "should screening ever run after hours" as a SEPARATE,
   later decision (it requires the price-band/stale-quote evaluation named
   above), not bundle it in because collection and screening happen to live
   in the same source file today.

5. External-service safety: EDGAR's own `_RateLimiter`
   (`agent/edgar.py`, "10 requests per second... regardless of the number
   of machines used", the actual SEC fair-access limit) and
   `agent.edgar_collector.collect_filings`'s own accession-number dedup
   already hold regardless of what wall-clock time collection runs at —
   this codebase already collects overnight-published EDGAR filings during
   the FIRST in-session cycle after they post today, just delayed until
   the next open; running collection continuously would only change WHEN
   that fact enters the store, not add any new class of external-service
   risk. Alpaca's market-data endpoints (`agent/market_data_collector.py`)
   are read-only quote/bar endpoints, not order-dependent, and remain
   reachable outside RTH by design (this is exactly the "after-hours quote"
   data referenced in section 2 above).

6. Evidence-store safety: `agent.store.FactStore` is bitemporal,
   append-only, and already written to under exactly this shape (`as_of`,
   never `UPDATE`/`DELETE`) regardless of when a fact was `observed_at` —
   nothing about running collection outside a session changes any
   append-only/bitemporal invariant; a fact collected at 9pm is simply a
   fact whose own `observed_at` says 9pm, which `as_of(t)` already handles
   correctly today.

## 4. What this document deliberately does NOT do

- Does not implement `run_collection_only`, the new config flag, or the
  `run_loop` cadence branch above — left for a separate unit, per this
  unit's own instructions.
- Does not add a real news provider (explicitly out of scope — this unit's
  own instructions: "Do NOT add a news provider in this unit";
  `agent.news_provider.NullNewsProvider` remains the only implementation).
- Does not touch `agent/approval_execution.py`'s missing session gate on
  order submission (section 1's "related, independently-discovered
  finding") — that is an execution-path change, outside this item's scope
  and outside tonight's DO-NOT list boundary, and is instead carried
  forward as a disclosed finding for the final report.
- Does not change `run_loop.py`'s existing `in_session_now` gate for
  reconciliation in any way — every existing test in
  `tests/test_run_loop.py` describing that gate remains the correct,
  unchanged description of this system's behavior after this document.
