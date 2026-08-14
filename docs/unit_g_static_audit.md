# Unit G: full validation + static secret/bypass audit (reconstructed)

Status: independently reproduced now. This unit is the final validation
pass over everything reconstructed this session (Units A-F), plus a
static grep sweep of the codebase for hardcoded secrets, placeholder
credentials, and known bypass patterns. No prior Unit G findings were
recoverable from the lost transcript in specific-enough form to reproduce
as stated; this is new work.

## 1. Full test suite

- `pytest -q`: **4876 passed**, 0 failed.
- `node --test tests/*.js`: **34 passed**, 0 failed (4 test files:
  `dashboard_bind.js` polling logic and related JS coverage).

Both run clean at the end of this unit, after Units A-F's cumulative
changes (baseline 4870 -> 4876 across Units C/D; JS suite untouched by
any of this session's work).

## 2. Static secret audit — clean, no findings

Grep sweep across `agent/` and `scripts/` (excluding `tests/`), each
search chosen to match the specific patterns the controlling instructions
named:

- **Hardcoded Alpaca key prefixes** (`AK.../PK...` literal strings): none
  found.
- **`APCA-API-SECRET-KEY`/`APCA-API-KEY-ID` literal values**: every
  occurrence is a header-name string paired with a dynamically-resolved
  value (`self._credentials.key_id`, `self._secrets.resolve(...)`) in
  `agent/broker/alpaca.py`, `agent/broker/alpaca_market_data.py`, and the
  probe/preflight scripts -- never a literal credential value.
- **Hardcoded `Authorization: Bearer ...` headers**: none found.
- **Literal `password = "..."` assignments** outside test fixtures: none
  found.
- **`alpaca_secret_key`/`alpaca_api_secret`/`alpaca_key_id`** assigned a
  literal, non-placeholder value: none found (all occurrences are field
  names / config keys, resolved via `agent.secrets_provider` at runtime).
- **`gatekeeper-signing-key`/`gatekeeper_signing_key`** hardcoded values:
  none found; every occurrence reads `gatekeeper.signing_key` (the
  in-process attribute) or documents the keychain entry NAME (a string
  key, not a secret value) for `--signing-key-secret-ref`.
- **`YOUR_CURRENT` placeholder text**: no matches anywhere in the tree.
- **`TODO`/`FIXME` markers** in `agent/`, `scripts/`, `deploy/` (excluding
  tests): no matches.
- **Sample/demo/fallback credential-shaped literals** (`demo_key =
  "..."`, `sample_secret = "..."`, etc.): none found.

## 3. Static bypass audit — one previously-reported-and-confirmed finding, no new bypasses

- **Manual ledger mutation outside `agent.ledger.Ledger`'s own methods**:
  the only direct `._positions[...]` writes outside `Ledger` itself are
  in `agent/broker/simulator.py`, which is `SimulatorBroker`'s own
  internal simulated-broker-side state (a different object entirely,
  reconciled against `Ledger` elsewhere, not a way of mutating the local
  ledger directly) -- not a bypass.
- **Reconciliation/materiality bypass flags** (`skip_reconciliation`,
  `bypass_materiality`, etc.): no matches anywhere in the tree.
- **Broker order-submission call sites**: exactly ONE production call
  site invokes `adapter.submit(...)` -- `agent/approval_execution.py`,
  the single gate-approved execution path (invariant #2: one code path
  from store to orders). No other caller in `agent/` or `scripts/`
  reaches `.submit(`/`.submit_order(`/`.place_order(` directly.
- **Broker adapter construction sites**: `AlpacaPaperAdapter` is
  constructed directly (not through `agent.broker.selection.
  select_broker_adapter`) in `scripts/run_agent.py` (two call sites) and
  several probe/preflight/diagnostic scripts. **Classification:
  previously reported and independently confirmed** -- this exact
  divergence is already documented, with its own reasoning, in
  `agent/broker/selection.py`'s own module docstring ("`scripts/
  run_agent.py` is NOT wired through it in this same commit -- see this
  unit's own report for why... This is a genuine, verified conflict
  between two of this unit's own requirements, not an assumption --
  reported, not resolved unilaterally"), and tracked as already-completed
  prior work (task #267, "resolve/report run_agent.py wiring conflict").
  Nothing new to report here; re-confirmed the documented conflict still
  matches current source and is still openly disclosed, not silently
  papered over.
- **T4 analysis module never touches config/credentials/capability/
  reserve/risk**: `agent/analysis.py`'s own imports were read directly
  (not grepped for keywords) -- it imports only `analysis_cache`,
  `analysis_output`, `analysis_prompt`, `cost`, `edgar_collector`,
  `model_client`, `store`. No `config`, `secrets_provider`, or any
  capability/risk-writing module is imported, confirming invariant #6
  (the LLM extracts features and writes rationale; it never forecasts,
  sizes, authorizes, or touches config, credentials, capability status,
  reserve settings, or risk maxima) by import-graph inspection, not just
  by docstring claim.
- **No secret values are ever passed to `print`/`logging`**: the only
  `print(...)` calls anywhere near the word "secret" are two identical
  CLI help-text examples in `scripts/run_agent.py` showing an operator
  how to generate a fresh random token via Python's stdlib `secrets`
  module (`secrets.token_bytes(32).hex()`) -- unrelated to any resolved
  credential value, and printing a freshly-generated, not-yet-provisioned
  token is not a leak.

## Disclosed scope limit

This unit's static audit is a targeted grep sweep against the specific
patterns named in the controlling instructions, not a full line-by-line
security review of every module. The capability-firewall/four-gate
default-deny design (§K/L) and the broader adversarial entry-point matrix
(§A-§S) were already independently built, tested, and delivered in a
prior, separate overnight unit (tasks #349-355, a 31-point report,
committed to the real repository before this session's data loss and
therefore never at risk from it) -- re-verifying that entire prior audit
from scratch was judged out of scope for this reconstruction unit's
"full validation" step, which this document reads as running the test
suites and performing the specific static sweep requested, not
re-deriving already-delivered, already-real work a second time.
