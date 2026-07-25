# AI Investment Agent — scaffold

Starting point for the v1.1 plan (Days 1–2, plus the correctness-critical cores
of Days 6–10). Zero third-party dependencies so it runs immediately:

    cd scaffold
    python -m pytest tests -q

## What is here

| Module | Plan ref | Role |
| --- | --- | --- |
| `agent/config.py` | §9.1 | Config schema, platform bounds, unknown-key rejection |
| `agent/store.py` | v1.0 §5 | Bitemporal append-only store, `as_of()` |
| `agent/audit.py` | §8 | Hash-chained, append-only audit log |
| `agent/policy.py` | §5 | Capability status model, four-gate check |
| `agent/risk.py` | §6.1 | Dual-basis reserve, portfolio constrainer, gate 2 |
| `agent/holding.py` | §4.1–4.2 | Versioned holding policy, lot eligibility, early exit |
| `agent/daytrade.py` | §4.4 | Rolling PDT guard |
| `agent/approval.py` | §9 | Single-use, price-banded approval token |
| `agent/pipeline.py` | §3, §5.1 | **Gate composition** — the one order money flows through |
| `agent/cost.py` | §8.2 | Cost ledger, budget states |
| `agent/entities.py` | §9.1 | Runtime entities, kept in step with the SQL by test |
| `agent/broker/base.py` | §1.2, §5.1 | The swap seam. Gate 4 + token consumption live here |
| `agent/broker/simulator.py` | Day 8 | Paper broker with T+1 settlement |
| `migrations/001_init.sql` | §9.1 | Schema, incl. a CHECK that live orders carry a token |

Two design points worth knowing before you extend it:

* **Gate 4 is in `BrokerAdapter.submit`, not in each adapter.** Concrete adapters
  implement `_submit_impl`, which is only reached after the capability check has
  passed and — in live mode — after an approval token has been consumed. A new
  adapter inherits the backstop instead of having to remember it.
* **`agent/pipeline.py` is the only place that composes gates.** Nothing else
  should assemble its own sequence of checks; that is how a gate goes missing.

## What is deliberately NOT here

Collectors, the EDGAR/news feeds, the T3 materiality screen, the Claude analysis
call, the extraction cache and the dashboard — Days 4–5. The live broker adapter
— ~Day 20. The interfaces they plug into all exist.

## Invariants, and where each is enforced

| # | Invariant | Enforced by |
| --- | --- | --- |
| 1 | `as_of(t)` cannot return a fact with `observed_at > t` | `AsOfView` + assertion; property tests |
| 2 | Facts are never mutated or deleted | `StoreError` from `update`/`delete` |
| 3 | Audit rows are never mutated or deleted | `AuditError` from `_AppendOnlyList` (preventive) |
| 4 | The audit chain verifies from genesis | `AuditLog.verify` (detective backstop) |
| 5 | Reloading a persisted store is stable | `_load(persist=False)`; size-stability test |
| 6 | A lot's policy is frozen at fill | `HoldingPolicyRegistry`; version→duration is authoritative |
| 7 | Only settled *and* eligible lots are sellable | `sellable_qty`, `check_normal_exit`, pipeline holding gate |
| 8 | Reserve enforced from settled cash with an absolute floor | `risk_constrain` post-conditions; pipeline reserve gate |
| 9 | No disabled capability reaches execution | four gates: universe + pre-submit (pipeline), constrainer (risk), adapter (broker base) |
| 10 | An approval token is single-use, banded, expiring, non-reissuable | `ApprovalToken.consume`, `TokenReissued` |
| 11 | The fourth day trade in five sessions is blocked | `DayTradeGuard`; pipeline day-trade gate |
| 12 | Python entities and SQL cannot drift | `tests/test_entities_match_sql.py` |

## Next

1. `agent/broker/live.py` implementing `BrokerAdapter` — read methods plus
   `_submit_impl`. Do not override `submit`.
2. Days 4–5: collectors, market calendar, the T3 screen (pure local arithmetic,
   zero model calls), extraction cache.
3. An `import-boundary` test asserting the research package cannot import the
   live adapter — add it with the live adapter, not after.
