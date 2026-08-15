# scripts/fixtures/

Real capture, taken 2026-07-27T18:22:41Z (`capture_manifest.json`), run by
the operator on his own machine with real Alpaca paper credentials via
`scripts/alpaca_probe.py`. Files: `account.json`, `positions.json`,
`orders.json`, `activities.json`, `configurations.json`, `assets.json`
(SPY, QQQ, AAPL) -- each entry `{"status": <http status>, "body": <verbatim
response, credential-shaped fields redacted>}`.

**The account captured is brand new and has never traded**: `positions.json`,
`orders.json`, and `activities.json` are all `[]`. `account.json` shows a
$500 paper cash account (`multiplier: "1"`) created 2026-07-27, no margin.
This limits what the capture can answer -- see `agent/broker/alpaca.py`'s
module docstring and the delivery report for the full analysis. Summary:

1. **Settled vs. unsettled cash** -- CONFIRMED NO: `/v2/account` has 36
   top-level fields, none of them a settled/unsettled split. `AccountSnapshot`
   is unchanged; there's nothing to remap to. Still unobserved: whether
   `cash` itself moves differently around a real T+1 settlement, since this
   account has never sold anything.
2. **Status vocabulary** -- KNOWN-DEFERRED, not an open guess: `orders.json`
   is `[]`, because a brand-new account has no order history to observe.
   This isn't a gap in the probe -- a read-only script structurally cannot
   exercise order statuses without real fills existing first. `STATUS_MAP`
   stays as documented, judgment-call mappings until paper trading is
   actually running and has produced some order history to re-capture
   against.
3. **`supported_matrix()`** -- NOW PARTIALLY CONFIRMED/CONTRADICTED, from
   `configurations.json` and `assets.json` (SPY/QQQ/AAPL):
   - `session`: CONTRADICTED. The old `["REGULAR"]`-only guess was wrong --
     `configurations.json` reports `disable_overnight_trading: false`, and
     all three assets carry `overnight_tradable` + `fractional_eh_enabled`.
     Updated to `["REGULAR", "EXTENDED", "OVERNIGHT"]` (matches
     `agent.policy.initial_policy`'s own session vocabulary, which already
     disables EXTENDED/OVERNIGHT by policy -- this is a separate,
     broker-capability fact, not a proposal to enable them). Confirmed for
     3 symbols only, not the full tradable universe.
   - `fractional`: PARTIALLY CONFIRMED. `fractional_trading: true`
     (account) and `fractionable: true` (all 3 assets) confirm fractional
     trading is enabled and available. NOT confirmed: which order types
     accept a fractional quantity -- neither endpoint says, so
     `["MARKET", "LIMIT"]` is unchanged, still an unverified guess.
   - `order_type` / `time_in_force`: STILL UNVERIFIED. Neither endpoint
     exposes a supported-order-type or supported-TIF list; these are fixed
     API features this probe has no read-only way to check. Unchanged.
   - Incidental, unresolved tension (not modeled by any key): `account.json`
     says `shorting_enabled: false` account-wide, but `configurations.json`
     says `no_shorting: false` and all 3 assets say `shortable: true`. Not
     investigated further -- moot in practice, since shorting is
     independently disabled at the capability layer (Appendix E).

One more incidental, confirmed fact: `account.json` omits `pattern_day_trader`
and `daytrade_count` entirely (not `false`/`0` -- absent), while every other
boolean flag on the account (`trading_blocked`, `shorting_enabled`, etc.) is
present even when false. `AccountSnapshot` now models both as `bool | None`/
`int | None` (`None` = unknown) instead of silently defaulting to
`False`/`0` -- see `agent/broker/alpaca.py`'s `account()` and
`agent/daytrade.py`'s `DayTradeGuard.reconcile`.

## Second capture, 2026-07-30T22:57:57Z (`capture_manifest.json`, same file, updated)

The account has now traded: a $500 JNLC deposit, a fractional SPY BUY
(0.027087234 sh @ 737.986), and a CAT (Consolidated Audit Trail) regulatory
FEE of -$0.01 against that fill, posted overnight. `account.json`,
`positions.json`, `orders.json`, and `activities.json` are all refreshed to
this state (no longer the brand-new-account `[]` captures above).
`activities_since.json` is new: the output of `--activities-since
2026-07-28 --direction asc` (paginated, all types, no `activity_types`
filter).

**`--activities-since` is looser than its name suggests.** Called with
`--activities-since 2026-07-28`, `activities_since.json` still includes the
JNLC deposit dated 2026-07-27 -- one day before the given cutoff. Alpaca's
`after` query param on `/v2/account/activities` is evidently not a strict
"activities on or after this date" filter in the way the flag name implies;
the exact boundary semantics (transaction time vs. settlement date vs.
something else) were not tracked down further, since chasing that isn't
this capture's purpose. Noted here as a fixture-fidelity caveat only --
`scripts/alpaca_probe.py` itself is unchanged; this is not a probe defect,
just a documented mismatch between the flag's name and Alpaca's own filter
behavior.

## SYNTHETIC TEST DATA (security-remediation unit, 2026-08-15)

**LOW finding, Codex Security scan**: "real broker captures tracked as
fixtures" -- these files (`account.json`, `positions.json`, `orders.json`,
`activities.json`, `activities_since.json`) are a real capture against a
real (paper) Alpaca account and contain PERSISTENT, correlatable
identifiers: the account's own immutable `id` (a UUID), its human-readable
`account_number`, and several real order/execution/activity ids and
timestamps. **These are not credentials** -- nothing in these files can be
used to authenticate as the account (the actual API key id and secret are
redacted at capture time by `scripts/alpaca_probe.py`'s own `_redact`, and
independently verified never to leak by `scripts/fixture_privacy_scan.py`,
run automatically as part of the test suite via
`tests/test_fixture_privacy_scan.py`) -- but they do persistently identify
a specific real account.

**What changed**: `tests/test_broker_alpaca.py` used to read several of
these files directly and assert against their real identifiers (a real
account UUID, a real `account_number`, real order/execution ids). Those
tests were rewritten to use SYNTHETIC, fabricated data that reproduces the
exact real wire SHAPE (field names, the notional-order null-qty quirk, the
FILL-row-has-no-created_at quirk) without depending on or exposing the
real captured values -- see that file's own "SYNTHETIC TEST DATA" comment
block, immediately above the tests it applies to.

**What did NOT change**: these fixture files themselves. Per this
codebase's own "do not delete evidence blindly" posture (this unit's own
explicit instruction), they remain committed, unmodified, as the original
probe evidence the rest of this README documents. If broader scrubbing of
the real account_number/UUID (which are also reused, as plain string
constants, in several unrelated test files elsewhere in this codebase's
test suite -- NOT by reading these fixture files, just as realistic-
looking literal values, the same way `"acct-a"` or any other test
constant would be) is wanted, that is a separate, deliberately out-of-
scope decision for a human to make, not something this unit did
unilaterally.
