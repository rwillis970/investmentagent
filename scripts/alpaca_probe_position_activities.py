#!/usr/bin/env python3
"""Empirical probe: does Alpaca report a quantity/share-count field on any
NON-FILL Account Activity that carries a symbol (Unit 12.5, 2026-08-12)?

WHY THIS EXISTS. `agent/broker/base.py`'s `AccountActivity` (what
`BrokerAdapter.non_fill_activities()` returns, and what `agent.cash_events.
sync_cash_events` already consumes) has no quantity field -- only
`net_amount`, a signed DOLLAR figure. Unit 12 ("positions: per-cycle sync
from non-fill activities") needs a real per-share quantity for a
`PositionEvent` sourced from a dividend-reinvestment, corporate
reorganization, or similar non-FILL activity -- and stopped, unbuilt,
specifically because nothing in this codebase has ever confirmed Alpaca
actually reports one anywhere in that endpoint's response. This script is
the confirmation step, mirroring `scripts/alpaca_probe.py`'s own "replace
the guess with a raw, verbatim capture" discipline (see that module's own
docstring) -- it does NOT modify `AccountActivity` or the Alpaca parser;
per this unit's own instruction, that is explicitly out of scope here.

REUSES `scripts/alpaca_probe.py`'s OWN PAGINATION AND REDACTION, RATHER
THAN A SECOND COPY. `_fetch_all_activities_since`/`_redact`/`_get`/
`BASE_URL` are imported directly from that module -- confirmed already
correct against Alpaca's own documented `/v2/account/activities`
pagination contract (that module's own docstring, "confirmed directly
against Alpaca's own docs today, 2026-07-30"), and re-deriving the same
walk here would be exactly the "one implementation, not two" duplication
this codebase's own control architecture avoids elsewhere (see e.g.
`BrokerAdapter.sessions()`/`market_calendar.trailing_sessions`).

TWO VIEWS OF THE SAME MATCHING ACTIVITIES, SIDE BY SIDE. For every activity
in the `--activities-since` window whose `activity_type` is NOT `"FILL"`
and whose `symbol` is present (non-null, non-empty):

  1. THE RAW VIEW -- the verbatim (redacted) JSON dict Alpaca returned,
     every key it sent, exactly as received. This is the one that actually
     answers the question: if Alpaca sends a quantity-shaped key on any
     such row, it is visible here even though `AccountActivity`'s own
     parser (agent/broker/alpaca.py) would silently drop it today.
  2. THE PARSED VIEW -- the SAME activities, but run through the real,
     already-deployed `agent.broker.alpaca.AlpacaPaperAdapter.
     non_fill_activities()` (constructed against the SAME credentials,
     hitting the SAME account), so a human can see, side by side, exactly
     what this system currently keeps versus what the broker actually
     sent -- matched by `activity_id`.

READ-ONLY, BY CONSTRUCTION, NOT BY COMMENT -- identical posture to
`scripts/alpaca_probe.py`: every HTTP call here goes through that module's
own `_get`/`_fetch_all_activities_since`, both hardcoded to GET. The one
extra call this script makes beyond a raw GET is constructing a real
`AlpacaPaperAdapter` and calling its `.non_fill_activities()` method (view
2 above) -- also a read (GET `/v2/account/activities`, filtered
client-side), never a write; `AlpacaPaperAdapter` has no method this script
calls that could place, cancel, or modify anything.

NOT PART OF THE RUNTIME PACKAGE -- same posture as `scripts/alpaca_probe.py`
(see that module's own docstring): nothing under `agent/` imports this
file, and it is a one-off operator tool, run manually, once, by a human
with real Alpaca paper credentials already provisioned in the OS keychain.

USAGE (run manually, by a human, on a machine that (a) has real Alpaca
paper credentials already stored in the login keychain under mode PAPER,
and (b) has outbound network access to paper-api.alpaca.markets -- NEITHER
is available in the sandbox this script was written in, which is why this
unit stops here rather than reporting a captured result):

    python scripts/alpaca_probe_position_activities.py \\
        --key-id <your Alpaca paper key id> \\
        --account-id <local account id label -- see note below> \\
        --secret-ref <keychain account name the secret is stored under> \\
        --out scripts/fixtures/ [--activities-since 2026-07-28]

`--account-id` is NOT read from Alpaca -- it is the same local label
`AlpacaPaperAdapter.__init__`/`BrokerCredentials` require to construct the
adapter for the PARSED VIEW above (the adapter stamps whatever it is given
onto every `AccountActivity.account_id` it returns; Alpaca's own API scopes
activities to the caller's credentials implicitly, not by an account_id
query parameter). Use the same value your real `--account-id` flag on
`scripts/run_agent.py` uses.

OUTPUT: writes `non_fill_activities_with_symbol.json` into `--out`, shaped
`{"captured_at", "base_url", "activities_since", "raw_activity_count_total",
"matching_count", "matching_activity_types", "raw": [...], "parsed": [...]}`.
`raw` is the redacted, verbatim JSON for every matching activity, in the
order Alpaca returned them; `parsed` is the SAME activities' worth of
`AccountActivity` dicts (`activity_id` keyed, for matching against `raw`).
Prints a one-line summary per matching row to stdout, including
`sorted(row.keys())`, specifically so a human can eyeball for a
quantity/share-count-shaped key without opening the JSON file. Writes the
file (with `matching_count: 0`) and prints a plain "found none" message
even if the window contains no non-FILL, symbol-bearing activity at all --
per this unit's own "stop after the probe, even if the output is empty or
negative" instruction.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.accounts import BrokerCredentials
from agent.broker.alpaca import AlpacaPaperAdapter
from agent.broker.transport import Transport, UrllibTransport
from agent.secrets_provider import KeychainSecretsProvider, SecretsProvider
from scripts.alpaca_probe import (BASE_URL, DEFAULT_ACTIVITIES_SINCE, _fetch_all_activities_since,
                                  _get, _redact)


def _account_activity_to_dict(act) -> dict:
    """`AccountActivity` (agent/broker/base.py) has `Decimal`/`date`/
    `datetime` fields, none JSON-native -- the same str()/isoformat()
    discipline `agent/ledger_store.py`'s own `_encode_fill` etc. already
    use, applied here so `json.dumps` doesn't need a custom encoder for a
    one-off operator script."""
    d = asdict(act)
    d["net_amount"] = str(act.net_amount)
    d["date"] = act.date.isoformat()
    d["created_at"] = act.created_at.isoformat() if act.created_at else None
    return d


def probe_position_activities(
    key_id: str, account_id: str, secret_ref: str, out_dir: Path, *,
    activities_since: str = DEFAULT_ACTIVITIES_SINCE,
    transport: Transport | None = None,
    secrets_provider: SecretsProvider | None = None,
) -> dict:
    """Fetch every Account Activity since `activities_since` (raw, via
    `scripts.alpaca_probe._fetch_all_activities_since`), filter to
    `activity_type != "FILL"` and a truthy `symbol`, and write both the raw
    and parsed views of exactly those rows. Returns the written payload for
    the caller's own summary -- same "return what was written, for the
    caller to print" shape `scripts.alpaca_probe.probe` uses.

    `transport`/`secrets_provider` default to the real `UrllibTransport`/
    `KeychainSecretsProvider` -- injectable for tests, same as
    `scripts.alpaca_probe.probe`."""
    provider = secrets_provider or KeychainSecretsProvider(mode="PAPER")
    secret = provider.resolve(secret_ref)
    headers = {
        "APCA-API-KEY-ID": key_id,
        "APCA-API-SECRET-KEY": secret,
        "Content-Type": "application/json",
    }
    real_transport = transport or UrllibTransport()

    raw_activities = _fetch_all_activities_since(real_transport, headers, after=activities_since)
    redacted_raw = _redact(raw_activities)
    matching_raw = [
        row for row in redacted_raw
        if row.get("activity_type") != "FILL" and row.get("symbol")
    ]
    matching_ids = {row["id"] for row in matching_raw if "id" in row}

    # PARSED VIEW: the real, already-deployed adapter method, filtered
    # client-side down to the same activity_ids the raw pass matched --
    # never a second, independent parse of the raw JSON (that would be
    # exactly the thing this script exists to check FOR, not to redo).
    credentials = BrokerCredentials(account_id=account_id, key_id=key_id, secret_ref=secret_ref)
    adapter = AlpacaPaperAdapter(account_id=account_id, credentials=credentials,
                                 secrets_provider=provider, transport=real_transport)
    parsed_matching = [
        _account_activity_to_dict(act) for act in adapter.non_fill_activities()
        if act.activity_id in matching_ids
    ]

    matching_types: dict[str, int] = {}
    for row in matching_raw:
        t = row.get("activity_type", "UNKNOWN")
        matching_types[t] = matching_types.get(t, 0) + 1

    payload = {
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "base_url": BASE_URL,
        "activities_since": activities_since,
        "raw_activity_count_total": len(raw_activities),
        "matching_count": len(matching_raw),
        "matching_activity_types": matching_types,
        "raw": matching_raw,
        "parsed": parsed_matching,
    }

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "non_fill_activities_with_symbol.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True))

    if not matching_raw:
        print(
            f"no non-FILL, symbol-bearing activities found in "
            f"{len(raw_activities)} activities since {activities_since!r} -- "
            "wrote matching_count: 0. This means either this account has never "
            "had one (try an earlier --activities-since), or Alpaca genuinely "
            "never posts a symbol on a non-FILL activity type -- cannot "
            "distinguish those two from this capture alone."
        )
    for row in matching_raw:
        print(
            f"MATCH activity_id={row.get('id')!r} type={row.get('activity_type')!r} "
            f"sub_type={row.get('activity_sub_type')!r} symbol={row.get('symbol')!r} "
            f"keys={sorted(row.keys())}"
        )

    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--key-id", required=True, help="Alpaca paper API key id (not the secret)")
    parser.add_argument(
        "--account-id", required=True,
        help="local account id label for AlpacaPaperAdapter's parsed view -- "
             "not read from Alpaca; see module docstring",
    )
    parser.add_argument(
        "--secret-ref", required=True,
        help="keychain account name the API secret is stored under, resolved via "
             "SecretsProvider -- never pass the raw secret value on the command line",
    )
    parser.add_argument("--out", default="scripts/fixtures", help="output directory")
    parser.add_argument(
        "--activities-since", default=DEFAULT_ACTIVITIES_SINCE,
        help="pull every Account Activity (all types, paginated) created on or "
             f"after this date (YYYY-MM-DD); default: {DEFAULT_ACTIVITIES_SINCE}",
    )
    args = parser.parse_args()
    probe_position_activities(args.key_id, args.account_id, args.secret_ref, Path(args.out),
                              activities_since=args.activities_since)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
