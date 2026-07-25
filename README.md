# AI Investment Agent — scaffold

Starting point for the v1.1 plan (Days 1–2 plus the correctness-critical cores
of Days 6–9). Zero third-party dependencies so it runs immediately:

    cd scaffold
    python -m pytest tests -q

## What is here

| Module | Plan ref | Status |
| --- | --- | --- |
| `agent/config.py` | §9.1 | Config schema, bounds, unknown-key rejection |
| `agent/store.py` | §5 (v1.0), §1.1 | Bitemporal append-only store, `as_of()` |
| `agent/audit.py` | §8 | Hash-chained audit log |
| `agent/policy.py` | §5 | Capability status model + four-gate check |
| `agent/risk.py` | §6.1 | Dual-basis reserve, portfolio constrainer |
| `agent/holding.py` | §4.1–4.2 | Lot-level eligibility, early-exit workflow |
| `agent/daytrade.py` | §4.4 | Rolling PDT guard |
| `agent/approval.py` | §9 | Single-use approval token |
| `agent/cost.py` | §8.2 | Cost ledger + budget states |
| `agent/broker/base.py` | §1.2 | The swap seam. Posture is *detected*, not declared |
| `agent/broker/simulator.py` | §11 Day 8 | Paper broker with T+1 settlement |

## What is deliberately NOT here

Collectors, the materiality screen, the Claude analysis call, the dashboard and
the live broker adapter. Those are Days 4–5 and ~Day 20; the interfaces they
plug into exist.

## Invariants the tests enforce

1. `store.as_of(t)` cannot return a fact with `observed_at > t`.
2. Facts and audit rows cannot be mutated or deleted.
3. The audit chain verifies from genesis.
4. A lot's holding policy is frozen at fill; shortening the policy never
   releases an already-open lot.
5. Only settled *and* hold-eligible lots are sellable.
6. Reserve is enforced from settled cash with an absolute floor.
7. No disabled capability passes the gate, for any dimension combination.
8. An approval token is single-use, price-banded and expiring.
9. The fourth day trade in five sessions is blocked.

## Next

`agent/broker/live.py` implementing `BrokerAdapter` against the chosen broker.
Nothing else should need to change.
