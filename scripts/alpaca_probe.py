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
     live payload contradict any of the five uncertain mappings?
  3. Does the account's own metadata (fractional eligibility, supported
     time-in-force, extended-hours flags) confirm or contradict the static
     `supported_matrix()` guess?

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
        --out scripts/fixtures/

This writes four JSON files (account.json, positions.json, orders.json,
activities.json) plus capture_manifest.json (capture timestamp, base URL,
endpoints hit) into --out, and prints a one-line summary per endpoint to
stdout.

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

from agent.broker.transport import UrllibTransport
from agent.secrets_provider import KeychainSecretsProvider

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
)


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


def probe(key_id: str, secret_ref: str, out_dir: Path) -> dict:
    """Resolve real paper credentials via `KeychainSecretsProvider`, GET
    each of the four endpoints in `_ENDPOINTS`, write each raw (redacted)
    response to `out_dir`, and write a capture manifest alongside them.
    Returns the in-memory results too, for the caller's own summary."""
    provider = KeychainSecretsProvider(mode="PAPER")
    secret = provider.resolve(secret_ref)
    headers = {
        "APCA-API-KEY-ID": key_id,
        "APCA-API-SECRET-KEY": secret,
        "Content-Type": "application/json",
    }
    transport = UrllibTransport()

    out_dir.mkdir(parents=True, exist_ok=True)
    captured_at = datetime.now(timezone.utc).isoformat()
    results: dict[str, dict] = {}

    for name, path, params in _ENDPOINTS:
        status, body = _get(transport, headers, path, params=params)
        payload = {"status": status, "body": _redact(body)}
        (out_dir / f"{name}.json").write_text(json.dumps(payload, indent=2, sort_keys=True))
        results[name] = payload
        print(f"{name}: HTTP {status}, {len(json.dumps(body))} bytes captured")

    manifest = {
        "captured_at": captured_at,
        "base_url": BASE_URL,
        "endpoints": [f"{path}?{params}" if params else path for _, path, params in _ENDPOINTS],
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
    args = parser.parse_args()
    probe(args.key_id, args.secret_ref, Path(args.out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
