#!/usr/bin/env python3
"""Empirical probe of a real Alpaca PAPER account (§1.2, §11 Day 10).

WHY THIS EXISTS: `agent/broker/alpaca.py` made three judgment calls that
could only be guessed at, not confirmed, without hitting a real account --
see that module's docstring. This script replaces the guesses with a raw,
verbatim capture of what the account actually returns, so a human can
answer, from evidence:

  1. Settled vs unsettled cash -- is there any field that distinguishes
     them in a cash account?
  2. Which of the 17 modeled order statuses actually appear, and does the
     live payload contradict any of the five uncertain mappings? (Needs
     real order history -- see the 2026-07-27 fixture README: this remains
     a known-deferred question, not an open guess, until paper trading is
     actually running and produces fills.)
  3. Does `supported_matrix()`'s guess about fractional eligibility,
     supported time-in-force, and extended-hours match reality? The FIRST
     capture (2026-07-27) found `/v2/account` carries none of this -- it
     lives on `/v2/account/configurations` (fractional_trading, no_shorting,
     pdt_check) and per-symbol `/v2/assets/{symbol}` (fractionable,
     shortable, marginable). This script now hits both.
  4. What actually caused a real settled-cash figure to move from
     Decimal('480.01') to Decimal('480') overnight, with the market closed
     and no new fill (found running the loop, 2026-07-29)? Not float noise
     (the Decimal migration ruled that out) -- a real external cash
     movement. `_fetch_all_activities_since` pulls EVERY Account Activity
     (FILL and every non-trade type -- CSD, CSW, FEE, INT, DIV, and
     whatever else the account actually reports) since a given date,
     unfiltered by type, so the actual cause can be read off the response
     rather than guessed at. See that function's own docstring for the
     pagination contract, confirmed directly against Alpaca's docs today
     (2026-07-30), and DEFAULT_ACTIVITIES_SINCE below.

READ-ONLY, BY CONSTRUCTION, NOT BY COMMENT. Every HTTP call in this file
goes through `_get()`, and `_get()` is hardcoded to call
`transport.request("GET", ...)` -- there is no function anywhere in this
module capable of issuing a POST, PUT, PATCH or DELETE, and no order,
cancellation or modification is ever constructed. Search this file for the
strings "POST", "PUT", "PATCH", "DELETE": they appear only in this
docstring, in prose, explaining their absence -- never as an argument to
anything.

NOT PART OF THE RUNTIME PACKAGE. Nothing under `agent/` imports this file,
no `__init__.py` exports it, and it is not loaded by config or startup. It
is a one-off operator tool: a human runs it manually, once (or occasionally,
to refresh the fixture), with real paper credentials that are already
provisioned in the OS keychain -- this script does not provision anything,
only resolves what is already there via `KeychainSecretsProvider`. See
`agent/secrets_provider.py` for how those entries get there (out of band,
via the `security` CLI directly).

USAGE (run manually, by a human, on a machine that (a) has real Alpaca
paper credentials already stored in the login keychain under mode PAPER,
and (b) has outbound network access to paper-api.alpaca.markets):

    python scripts/alpaca_probe.py --key-id <your Alpaca paper key id> \\
        --secret-ref <keychain account name the secret is stored under> \\
        --out scripts/fixtures/ [--symbols SPY,QQQ,AAPL] \\
        [--activities-since 2026-07-28]

This writes account.json, positions.json, orders.json, activities.json,
configurations.json (`/v2/account/configurations`), assets.json (one entry
per `--symbols` symbol, from `/v2/assets/{symbol}`), activities_since.json
(EVERY Account Activity, every type, created on or after `--activities-since`
-- see `_fetch_all_activities_since`'s own docstring), and
capture_manifest.json (capture timestamp, base URL, every endpoint hit)
into --out, and prints a one-line summary per endpoint to stdout.
`--symbols` defaults to `DEFAULT_SYMBOLS` below -- a small, fixed set, not
the whole tradable universe. `--activities-since` defaults to
`DEFAULT_ACTIVITIES_SINCE` below.

REDACTION: `_redact` recursively blanks any dict value whose key looks
credential-shaped (secret, api_key, password, token, and Alpaca's own
header-cased variants). In practice Alpaca's account/position/order/
activity responses do not echo the API key or secret back, so this is
expected to be a no-op against real data -- it is applied anyway, because
the instruction was to dump every field verbatim EXCEPT anything that is
itself a credential, and this is what makes that true even if a future
Alpaca response ever changed shape.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.broker.transport import Transport, UrllibTransport
from agent.secrets_provider import KeychainSecretsProvider, SecretsProvider

BASE_URL = "https://paper-api.alpaca.markets"

_CREDENTIAL_LIKE_KEYS = {
    "secret", "api_key", "apca-api-key-id", "apca-api-secret-key",
    "password", "token", "secret_key", "access_token",
}

_ENDPOINTS = (
    ("account", "/v2/account", None),
    ("positions", "/v2/positions", None),
    ("orders", "/v2/orders", {"status": "all"}),
    ("activities", "/v2/account/activities", None),
    ("configurations", "/v2/account/configurations", None),
)

# A small, fixed set -- not the tradable universe. Enough to see whether
# `fractionable`/`shortable`/`marginable` vary at all across a couple of
# ordinary large-cap names, without turning this into a universe crawl.
DEFAULT_SYMBOLS: tuple[str, ...] = ("SPY", "QQQ", "AAPL")

# The date the Decimal-migration fix went in and the settled-cash-halt
# investigation this capture exists for actually starts -- named, not
# guessed. Overridable via --activities-since for any future investigation.
DEFAULT_ACTIVITIES_SINCE = "2026-07-28"


def _redact(obj):
    """Recursively blank any dict value whose key looks credential-shaped.
    Defensive, expected no-op against real Alpaca responses -- see module
    docstring."""
    if isinstance(obj, dict):
        return {
            key: ("***REDACTED***" if key.lower() in _CREDENTIAL_LIKE_KEYS else _redact(value))
            for key, value in obj.items()
        }
    if isinstance(obj, list):
        return [_redact(item) for item in obj]
    return obj


def _get(transport, headers, path, *, params=None, timeout=10.0):
    """The only function in this module that makes an HTTP call. Hardcoded
    to method="GET" -- there is no write path to remove here, because there
    was never one to begin with."""
    status, body = transport.request(
        "GET", f"{BASE_URL}{path}", headers=headers, params=params, timeout=timeout,
    )
    return status, body


def _fetch_all_activities_since(transport, headers, *, after: str,
                                page_size: int = 100) -> list[dict]:
    """Every Account Activity created on or after `after`, oldest first --
    FILL and every non-trade type alike. Deliberately never passes
    `activity_types` or `category`: filtering by type before knowing what
    actually happened would risk hiding the answer this capture exists to
    find (module docstring, point 4 -- "do not invent the output" applies
    just as much to silently excluding a type as to fabricating one).

    Confirmed directly against Alpaca's own docs
    (docs.alpaca.markets/reference/getaccountactivities-2, fetched
    2026-07-30) for this exact endpoint
    (paper-api.alpaca.markets/v2/account/activities): `after`, `direction`,
    `page_size` and `page_token` are real, documented query params on it.
    Pagination walks forward with `direction=asc` (oldest first) and
    `page_token` set to the LAST-SEEN activity's own `id`, in batches of
    `page_size`, stopping the first time a page comes back shorter than
    `page_size` -- the documented signal that nothing is left. This is the
    same contract `agent.broker.alpaca.AlpacaPaperAdapter.fills()` already
    relies on for the FILL-only endpoint; re-confirmed here independently
    for the unfiltered one, not assumed to carry over.

    GET only, via `_get()` -- see module docstring's read-only guarantee."""
    activities: list[dict] = []
    page_token: str | None = None
    while True:
        params: dict = {"after": after, "direction": "asc", "page_size": str(page_size)}
        if page_token is not None:
            params["page_token"] = page_token
        _, data = _get(transport, headers, "/v2/account/activities", params=params)
        if not data:
            break
        activities.extend(data)
        if len(data) < page_size:
            break
        page_token = data[-1]["id"]
    return activities


def probe(key_id: str, secret_ref: str, out_dir: Path, *,
         symbols: tuple[str, ...] = DEFAULT_SYMBOLS,
         activities_since: str = DEFAULT_ACTIVITIES_SINCE,
         transport: Transport | None = None,
         secrets_provider: SecretsProvider | None = None) -> dict:
    """Resolve real paper credentials via `KeychainSecretsProvider` (or the
    injected `secrets_provider`, for tests), GET each of the endpoints in
    `_ENDPOINTS` plus one `/v2/assets/{symbol}` per entry in `symbols`,
    write each raw (redacted) response to `out_dir`, and write a capture
    manifest alongside them. Returns the in-memory results too, for the
    caller's own summary.

    `transport`/`secrets_provider` default to the real `UrllibTransport`/
    `KeychainSecretsProvider` when not supplied -- the same
    "inject for tests, default to real for the operator" shape
    `AlpacaPaperAdapter` uses (agent/broker/alpaca.py), added specifically
    so this orchestration is testable without a real account."""
    provider = secrets_provider or KeychainSecretsProvider(mode="PAPER")
    secret = provider.resolve(secret_ref)
    headers = {
        "APCA-API-KEY-ID": key_id,
        "APCA-API-SECRET-KEY": secret,
        "Content-Type": "application/json",
    }
    transport = transport or UrllibTransport()

    out_dir.mkdir(parents=True, exist_ok=True)
    captured_at = datetime.now(timezone.utc).isoformat()
    results: dict[str, dict] = {}
    endpoints_hit: list[str] = []

    for name, path, params in _ENDPOINTS:
        status, body = _get(transport, headers, path, params=params)
        payload = {"status": status, "body": _redact(body)}
        (out_dir / f"{name}.json").write_text(json.dumps(payload, indent=2, sort_keys=True))
        results[name] = payload
        endpoints_hit.append(f"{path}?{params}" if params else path)
        print(f"{name}: HTTP {status}, {len(json.dumps(body))} bytes captured")

    # EVERY Account Activity since `activities_since`, every type -- not
    # the single-shot, unfiltered-but-unpaginated `activities` entry above
    # (which silently truncates at 100 rows and carries no explicit date
    # filter). See `_fetch_all_activities_since`'s own docstring.
    since_activities = _fetch_all_activities_since(transport, headers, after=activities_since)
    redacted_since_activities = _redact(since_activities)
    activity_type_counts: dict[str, int] = {}
    for a in redacted_since_activities:
        t = a.get("activity_type", "UNKNOWN")
        activity_type_counts[t] = activity_type_counts.get(t, 0) + 1
    activities_since_payload = {
        "after": activities_since,
        "direction": "asc",
        "count": len(redacted_since_activities),
        "activity_type_counts": activity_type_counts,
        "activities": redacted_since_activities,
    }
    (out_dir / "activities_since.json").write_text(
        json.dumps(activities_since_payload, indent=2, sort_keys=True))
    results["activities_since"] = activities_since_payload
    endpoints_hit.append(
        f"/v2/account/activities?after={activities_since}&direction=asc (paginated, all types)"
    )
    print(f"activities_since: {len(redacted_since_activities)} activities captured, "
         f"types={activity_type_counts}")

    assets: dict[str, dict] = {}
    for symbol in symbols:
        path = f"/v2/assets/{symbol}"
        status, body = _get(transport, headers, path)
        assets[symbol] = {"status": status, "body": _redact(body)}
        endpoints_hit.append(path)
        print(f"assets[{symbol}]: HTTP {status}, {len(json.dumps(body))} bytes captured")
    (out_dir / "assets.json").write_text(json.dumps(assets, indent=2, sort_keys=True))
    results["assets"] = assets

    manifest = {
        "captured_at": captured_at,
        "base_url": BASE_URL,
        "endpoints": endpoints_hit,
        "note": "READ-ONLY capture. No order was placed, cancelled, or modified.",
    }
    (out_dir / "capture_manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True))
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--key-id", required=True, help="Alpaca paper API key id (not the secret)")
    parser.add_argument(
        "--secret-ref", required=True,
        help="keychain account name the API secret is stored under, resolved via "
             "SecretsProvider -- never pass the raw secret value on the command line",
    )
    parser.add_argument("--out", default="scripts/fixtures", help="output directory")
    parser.add_argument(
        "--symbols", default=",".join(DEFAULT_SYMBOLS),
        help="comma-separated symbols to GET /v2/assets/{symbol} for (small set, "
             f"not a universe crawl; default: {','.join(DEFAULT_SYMBOLS)})",
    )
    parser.add_argument(
        "--activities-since", default=DEFAULT_ACTIVITIES_SINCE,
        help="pull every Account Activity (all types, paginated) created on or "
             f"after this date (YYYY-MM-DD); default: {DEFAULT_ACTIVITIES_SINCE}",
    )
    args = parser.parse_args()
    symbols = tuple(s.strip() for s in args.symbols.split(",") if s.strip())
    probe(args.key_id, args.secret_ref, Path(args.out), symbols=symbols,
         activities_since=args.activities_since)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
