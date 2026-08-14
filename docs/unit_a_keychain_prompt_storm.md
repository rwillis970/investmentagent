# Unit A — Keychain prompt storm (reconstructed 2026-08-13)

STATUS OF PRIOR REPORT: a previous session reported this unit as investigated
and fixed, on a branch (`keychain-lock-phase23-audit`) inside a `/tmp`
worktree that no longer exists (the sandbox's local disk is ephemeral and was
recycled between sessions; nothing was ever copied to a persistent location,
per this unit's own rule against touching the real checkout). That prior
report's specific numbers are UNVERIFIED and are not restated here. Everything
below was independently re-derived from the CURRENT real-repo source in a
freshly created isolated worktree, with real, runnable tests as evidence.

## Root cause (independently reproduced now, from current source)

Three facts, each confirmed by reading the current, real implementation
(not assumed):

1. `agent/broker/selection.py::select_broker_adapter` calls
   `secrets_provider.resolve(credentials.secret_ref)` once, as a fail-fast
   presence check, before constructing an adapter (by the module's own
   docstring: "CREDENTIALS COME FROM `agent.secrets_provider` ONLY, RESOLVED
   FRESH, NEVER CACHED HERE").
2. `agent/broker/alpaca.py::AlpacaPaperAdapter._headers()` calls
   `self._secrets.resolve(...)` again, fresh, on **every** HTTP call
   (`.account()`, `.positions()`, `.open_orders()`) — a deliberate design
   choice documented in that module's own CREDENTIALS section, not a bug in
   isolation.
3. `agent/secrets_provider.py`'s `KeychainSecretsProvider` has never cached
   anything (module docstring: "RESOLVED FRESH, NEVER CACHED HERE") — every
   `.resolve()` call shells out to `/usr/bin/security find-generic-password`.

Multiplied together: one steady-state `scripts/run_dashboard.py::
_build_broker_state` call — i.e. one real `GET /api/state` — makes **4**
separate `.resolve()` calls against the same `secret_ref` (1 presence check +
3 HTTP calls). `dashboard/static/dashboard_bind.js` polls every
`POLL_INTERVAL_MS = 5000` (5 seconds), for the life of an open browser tab.
`agent/run_loop.py::run_cycle` has the identical multiplicative shape for its
own reconciliation cycle (documented explicitly in that module's own
docstring: "`SecretsProvider.resolve` is already called fresh on every real
HTTP request... constructing the adapter... is what makes credential
resolution happen every cycle, automatically" — treating this as a feature,
not naming the cost).

**Disclosed limit**: this sandbox has no macOS Keychain and cannot invoke
`/usr/bin/security` or observe a real GUI confirmation prompt. The "4 calls
per poll" figure is measured via a counting `SecretsProvider` double wired
through the exact real production call path (`select_broker_adapter` →
`AlpacaPaperAdapter._headers()`), not by literally counting GUI prompts.
Whether a given resolve call actually produces a visible OS prompt depends on
the target Keychain item's own ACL (see `agent/secrets_provider.py`'s own
KEYCHAIN MECHANISM section) — this cannot be verified from this environment
either. What is verified is the call-count multiplication that would drive
however many prompts that ACL setting produces.

## Measured lookup counts

- **Test**: `tests/test_run_dashboard.py::
  test_measured_resolve_count_per_steady_state_refresh_is_four_uncached`
  — measured via a counting `SecretsProvider` wrapping `InMemorySecretsProvider`,
  driven through the real `_build_broker_state` → `select_broker_adapter` →
  `AlpacaPaperAdapter` call path, `ScriptedTransport` for the HTTP layer (no
  real network).
- **Before fix**: 4 resolve() calls per steady-state dashboard refresh (once
  the ledger already has an opening balance — the very first-ever refresh
  costs 5, due to the one-time positions-seed branch). At the frontend's own
  5-second poll interval, that is 4 real Keychain subprocess invocations
  every 5 seconds, indefinitely, for as long as a dashboard tab stays open —
  independent of, and in addition to, whatever `scripts/run_agent.py`'s own
  `run_cycle` does on its own reconciliation cadence.
- **After fix**: `tests/test_run_dashboard.py::
  test_caching_secrets_provider_answers_three_steady_state_refreshes_with_one_real_resolve`
  — 1 real resolve() call total across a seed call plus 3 further
  steady-state refreshes (all within the cache's 300s TTL). Verified via the
  same counting double, now wrapped in the new `CachingSecretsProvider`.

## The fix

`agent/secrets_provider.py` gained `CachingSecretsProvider` — a bounded-TTL
(default 300s) cache wrapping any `SecretsProvider`, keyed by `secret_ref`
(no mode key needed — a provider is already bound to one mode structurally,
see that module's own long-standing isolation contract). A successful
resolve is cached; a `SecretNotFoundError` is **never** cached, so a
transient failure (e.g. the login keychain briefly locked after sleep, per
that module's own KEYCHAIN MECHANISM section) is retried on the very next
call rather than remembered as permanently absent — this preserves the
codebase's fail-safe-to-NO-TRADE discipline in the one direction that
matters (never silently papering over a genuine absence for longer than one
call; the only accepted risk is a resolved-present value going stale for up
to 300s, far shorter than any real, out-of-band credential-rotation
workflow).

`agent/secrets_provider.py::default_keychain_secrets_provider_factory(mode)`
is the new single production default — `CachingSecretsProvider(
KeychainSecretsProvider(mode))` — used as the `secrets_provider_factory`
default in BOTH `scripts/run_dashboard.py` and `scripts/run_agent.py` (one
function, not two independent changes to keep in sync — matches this
codebase's general "one code path" discipline). Both scripts previously
defaulted directly to bare `KeychainSecretsProvider`; that import is now
unused in both and was removed.

Tests: `tests/test_secrets_provider.py` gained 8 new tests for
`CachingSecretsProvider` in isolation (TTL hit/miss, expiry, never-caches-
absence, independent per-secret_ref caching, `mode` passthrough, `time.
monotonic` as the real default clock). `tests/test_run_dashboard.py` gained
3 new tests (the before/after measured-count pair above, plus the scripted-
transport helper they share).

## Answers to the specific questions asked

- **Whether the broker adapter is now reused in memory across polls**: NO —
  and this fix does not attempt to change that. `select_broker_adapter`
  still constructs a fresh `AlpacaPaperAdapter` object on every refresh
  (documented, deliberate — see `agent/run_loop.py`'s own module docstring:
  "the adapter is stateless in the way that matters"). What is now reused
  across polls is the **resolved secret value**, via the cache — the
  multiplicative Keychain-subprocess cost this unit exists to close, not the
  adapter object itself, which was never the cost.
- **Whether HTTP refreshes still invoke `/usr/bin/security`**: yes, on the
  first call after cache-miss or TTL expiry (every 300s, or on process
  start) — never eliminated entirely, by design (see fix rationale above).
- **Whether `gatekeeper-signing-key` is needed by ordinary dashboard reads**:
  NO, confirmed by absence — `grep -n "_resolve_gatekeeper_signing_key"
  scripts/run_dashboard.py` returns zero matches. That function exists only
  in `scripts/run_agent.py`, called from two one-shot sites: the
  `--submit-approved` CLI handler, and `main()` once at process startup
  (before `run_loop` begins) — never inside the per-cycle loop itself. This
  was already correctly structured before this unit; nothing needed fixing
  here, and it now also benefits from the cache incidentally, via the same
  shared `secrets_provider_factory` default.
- **Whether any Keychain ACL/security weakening was introduced**: NO — this
  fix touches only in-process Python state (a `dict` cache with a TTL); it
  never calls `security add-generic-password` or any ACL-modifying command,
  and provisioning remains explicitly out of scope for this module (per its
  own long-standing docstring).
- **Whether the user should still expect a Keychain prompt on process
  startup**: yes — the very first resolve of a process's lifetime is still a
  real, uncached lookup (cache starts empty).
- **Whether opening/refreshing the dashboard page should now produce (close
  to) zero additional prompts after that first one**: yes, for at least 300
  seconds (the default TTL) after the most recent successful resolve —
  proven by the after-fix test above (1 real resolve across 4 consecutive
  steady-state calls).

## Legacy credential-reference matrix

Every `resolve()` call site found by static search (`grep -rn "\.resolve("
agent/ scripts/`), and its post-fix caching status:

| Call site | Purpose | secret_ref | Cached after this fix? |
|---|---|---|---|
| `agent/broker/selection.py::select_broker_adapter` | presence pre-check | `credentials.secret_ref` (alpaca) | YES, if reached via either script's default factory |
| `agent/broker/alpaca.py::AlpacaPaperAdapter._headers()` | every real HTTP call | same | YES, same reason |
| `scripts/run_agent.py::_resolve_gatekeeper_signing_key` | Gatekeeper verification key | `--signing-key-secret-ref` | YES, same `secrets_provider` instance, resolved once per process already |

No other `.resolve()` call sites exist in `agent/` or `scripts/` as of this
worktree's baseline commit.
