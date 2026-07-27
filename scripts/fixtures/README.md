# scripts/fixtures/

Real capture, taken 2026-07-27T18:00:18Z (`capture_manifest.json`), run by
the operator on his own machine with real Alpaca paper credentials via
`scripts/alpaca_probe.py`. Files: `account.json`, `positions.json`,
`orders.json`, `activities.json`, each `{"status": <http status>, "body":
<verbatim response, credential-shaped fields redacted>}`. `configurations.json`
and `assets.json` (one entry per symbol) were added to the probe script in
a later commit and are not yet in this particular capture -- re-running the
script will add them.

**The account captured is brand new and has never traded**: `positions.json`,
`orders.json`, and `activities.json` are all `[]`. `account.json` shows a
$500 paper cash account (`multiplier: "1"`) created 2026-07-27, no margin,
shorting disabled. This limits what the capture can answer -- see
`agent/broker/alpaca.py`'s module docstring and the delivery report for the
full three-question analysis. Summary:

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
   against. Nothing to fix here; nothing to re-guess either.
3. **`supported_matrix()`** -- was NEITHER CONFIRMED NOR CONTRADICTED by the
   first capture: `/v2/account` doesn't carry fractional/time-in-force/
   extended-hours metadata at all. The probe script now also hits
   `/v2/account/configurations` and per-symbol `/v2/assets/{symbol}`
   (default symbols: SPY, QQQ, AAPL) specifically to answer this -- rerun
   the script and bring back `configurations.json`/`assets.json` for that
   analysis.

One incidental, confirmed fact worth flagging even though it isn't one of
the three questions: `account.json` omits `pattern_day_trader` and
`daytrade_count` entirely (not `false`/`0` -- absent), while every other
boolean flag on the account (`trading_blocked`, `shorting_enabled`, etc.) is
present even when false. `AccountSnapshot` now models both as `bool | None`/
`int | None` (`None` = unknown) instead of silently defaulting to
`False`/`0` -- see `agent/broker/alpaca.py`'s `account()` and
`agent/daytrade.py`'s `DayTradeGuard.reconcile`.
