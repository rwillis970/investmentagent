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
        --out scripts/fixtures/ [--symbols SPY,QQQ,AAPL]

This writes account.json, positions.json, orders.json, activities.json,
configurations.json (`/v2/account/configurations`), assets.json (one entry
per `--symbols` symbol, from `/v2/assets/{symbol}`), and
capture_manifest.json (capture timestamp, base URL, every endpoint hit)
into --out, and prints a one-line summary per endpoint to stdout.
`--symbols` defaults to `DEFAULT_SYMBOLS` below -- a small, fixed set, not
the whole tradable universe.

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


def probe(key_id: str, secret_ref: str, out_dir: Path, *,
         symbols: tuple[str, ...] = DEFAULT_SYMBOLS,
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
    args = parser.parse_args()
    symbols = tuple(s.strip() for s in args.symbols.split(",") if s.strip())
    probe(args.key_id, args.secret_ref, Path(args.out), symbols=symbols)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
