#!/usr/bin/env python3
"""READ-ONLY BROKER CREDENTIAL/AUTH PREFLIGHT (15-minute credential-hardening
sprint, 2026-08-13). Answers exactly one question, safely: is the Alpaca
credential path for this account correctly wired, and (optionally) does it
actually authenticate -- without ever printing a secret, a header, or a full
account payload.

STRUCTURALLY INCAPABLE OF SUBMITTING OR CANCELLING AN ORDER, same reasoning
and same shape as `scripts/diagnose_runtime.py`'s own module docstring: this
script never imports `agent.pipeline`/`agent.approval*`/`agent.pipeline_stage`,
never constructs a `Gatekeeper`, and the one `AlpacaPaperAdapter` it builds
has no `capability_policy`/`staging_key` attached, so `.submit()`/`.cancel()`
would raise before any network call even if something upstream tried.

WHAT `--auth-check` DOES AND DOES NOT DO. With `--auth-check`, this script
issues exactly ONE real HTTP call: `GET /v2/account` (the same read
`AlpacaPaperAdapter.account()` already makes for every other read-only tool
in this codebase). It reports PASS/FAIL/UNAVAILABLE classifications built
from the HTTP STATUS CODE alone (captured via the adapter's own
`shape_debug_sink`, the same safe hook `diagnose_runtime.py --debug-shapes`
uses) -- never from the response BODY's contents. Without `--auth-check`
(the default), this script makes ZERO network calls: it only reports
whether the Keychain entry it would use is present, never resolving or
using it.

NEVER PRINTS: the raw secret, the `APCA-API-SECRET-KEY`/`APCA-API-KEY-ID`
header VALUES (only whether they are present), or the full account payload
(only its shape, via the same `_shape_summary` `diagnose_runtime.py` uses).
`--key-id` itself is printed back (it is the public-ish identifier this
codebase's own `agent.accounts.BrokerCredentials` docstring already treats
as non-secret -- the same thing Alpaca's own dashboard displays in the
clear), never the secret.

SERVICE-NAME DERIVATION IS NOT GUESSED. `_service_name` is imported directly
from `agent.secrets_provider` -- the exact function `KeychainSecretsProvider`
itself calls -- so this script's report of "expected Keychain service" can
never drift from what a real resolve() attempt would actually query.

MODE VS. PERSISTED RUNTIME STATE. The `mode` this script namespaces the
Keychain lookup under is `--mode` (default `PAPER`), matching every other
credential-resolving entry point in this codebase (`scripts/run_agent.py`,
`scripts/run_dashboard.py`, `scripts/diagnose_runtime.py` all bind
`secrets_provider_factory(cfg.mode)`, where `cfg.mode` is config.json's
`"mode"` key -- the CONFIGURED broker/credential environment, e.g. "PAPER" --
never the PERSISTED RUNTIME operational mode PAUSED/DISABLED/RESEARCH/
PRODUCTION_ACTIVE that `agent.mode_store.ModeStore` tracks separately; see
that module and agent/mode.py's own CHAIN). This script does not read
`agent.mode_store.ModeStore` at all -- there is no code path here that
COULD let a persisted PAUSED runtime mode change which Keychain namespace
is queried, because runtime mode and credential-namespace mode are two
different values from two different sources, and this script only ever
reads the latter."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.accounts import BrokerCredentials
from agent.broker.alpaca import AlpacaPaperAdapter, AlpacaResponseError
from agent.broker.transport import TransportError
from agent.secrets_provider import (KeychainSecretsProvider, SecretNotFoundError,
                                    SecretsProvider, _service_name)

# -- classification --------------------------------------------------------
AUTH_PASS = "AUTH PASS"
AUTH_FAIL = "AUTH FAIL"
CREDENTIAL_MISSING = "CREDENTIAL MISSING"
NETWORK_UNAVAILABLE = "NETWORK UNAVAILABLE"
RATE_LIMITED = "RATE LIMITED"
BROKER_ERROR = "BROKER ERROR"


def _classify_status(status: int | None) -> str:
    """HTTP status -> a safe, human classification. `429` and `5xx` are
    DELIBERATELY never classified as an auth failure -- rate limiting and
    broker-side errors say nothing about whether this credential is valid,
    and conflating them with AUTH_FAIL would send an operator chasing the
    wrong fix (rotating a key that was never the problem)."""
    if status is None:
        return NETWORK_UNAVAILABLE
    if 200 <= status < 300:
        return AUTH_PASS
    if status in (401, 403):
        return AUTH_FAIL
    if status == 429:
        return RATE_LIMITED
    return BROKER_ERROR


def preflight(*, account_id: str, key_id: str | None, secret_ref: str | None,
             mode: str, auth_check: bool,
             secrets_provider_factory=KeychainSecretsProvider) -> dict:
    """The whole check, injectable end to end for tests (no real Keychain,
    no real network call unless the caller explicitly opts in). Returns a
    plain dict -- never an object holding the resolved secret anywhere,
    even transiently past this function's own stack frame."""
    report: dict = {
        "broker_adapter_type": "alpaca_paper",
        "endpoint_classification": "PAPER",  # AlpacaPaperAdapter.BASE_URL is
        # a fixed class attribute -- see that class's own docstring; there
        # is no runtime flag that could make this "LIVE".
        "base_url": AlpacaPaperAdapter.BASE_URL,
        "account_id_requested": account_id,
        "mode": mode,
        "key_id_present": bool(key_id),
        "secret_ref_name": secret_ref,
        "keychain_service": _service_name(mode),
    }
    if not key_id:
        report["key_id_present"] = False
    if not secret_ref:
        report["credential_status"] = CREDENTIAL_MISSING
        report["credential_detail"] = "no --secret-ref given"
        return report

    secrets_provider: SecretsProvider = secrets_provider_factory(mode)
    try:
        secrets_provider.resolve(secret_ref)
        # Discarded immediately -- never stored on `report`, never returned,
        # never logged. Presence is all this function needs.
    except SecretNotFoundError as exc:
        report["credential_status"] = CREDENTIAL_MISSING
        report["credential_detail"] = str(exc)   # carries mode/secret_ref only, never a value
        return report

    report["credential_status"] = "PRESENT"

    if not auth_check:
        return report
    if not key_id:
        report["auth_status"] = CREDENTIAL_MISSING
        report["auth_detail"] = "no --key-id given -- cannot attempt a real request"
        return report

    captured: dict = {}

    def _sink(summary: dict) -> None:
        captured.update(summary)

    credentials = BrokerCredentials(account_id=account_id, key_id=key_id, secret_ref=secret_ref)
    adapter = AlpacaPaperAdapter(account_id=account_id, credentials=credentials,
                                 secrets_provider=secrets_provider,
                                 shape_debug_sink=_sink)
    try:
        adapter.account()
        report["auth_status"] = AUTH_PASS
    except AlpacaResponseError:
        # The shape-debug sink already fired (a response WAS received) --
        # classify from the captured status, not from the exception's own
        # message (never trust an error body's own contents for this).
        report["auth_status"] = _classify_status(captured.get("http_status"))
    except TransportError as exc:
        report["auth_status"] = NETWORK_UNAVAILABLE
        report["auth_detail"] = type(exc).__name__
    report["http_status"] = captured.get("http_status")
    report["response_shape"] = {k: v for k, v in captured.items()
                                if k not in ("endpoint", "http_status")}
    return report


def _print_report(report: dict) -> None:
    print(f"broker_adapter_type: {report['broker_adapter_type']}")
    print(f"endpoint_classification: {report['endpoint_classification']} "
         f"({report['base_url']})")
    print(f"account_id_requested: {report['account_id_requested']}")
    print(f"mode: {report['mode']}")
    print(f"key_id: {'PRESENT' if report['key_id_present'] else 'MISSING'}")
    print(f"secret_ref: {report['secret_ref_name']!r}")
    print(f"keychain_service: {report['keychain_service']!r}")
    print(f"credential_status: {report['credential_status']}")
    if report.get("credential_detail"):
        print(f"  detail: {report['credential_detail']}")
    if "auth_status" in report:
        print(f"auth_status: {report['auth_status']}")
        if report.get("http_status") is not None:
            print(f"  http_status: {report['http_status']}")
        if report.get("response_shape"):
            print(f"  response_shape: {report['response_shape']}")
        if report.get("auth_detail"):
            print(f"  detail: {report['auth_detail']}")


def _parse_args(argv: list[str] | None):
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--account-id", required=True)
    p.add_argument("--key-id", default=None)
    p.add_argument("--secret-ref", default=None)
    p.add_argument("--mode", default="PAPER")
    p.add_argument("--auth-check", action="store_true",
                   help="issue exactly one real GET /v2/account and classify the "
                        "HTTP status -- AUTH PASS / AUTH FAIL / RATE LIMITED / "
                        "BROKER ERROR / NETWORK UNAVAILABLE. Off by default: zero "
                        "network calls unless explicitly requested.")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    report = preflight(account_id=args.account_id, key_id=args.key_id,
                       secret_ref=args.secret_ref, mode=args.mode,
                       auth_check=args.auth_check)
    _print_report(report)
    ok = report.get("credential_status") == "PRESENT" and report.get(
        "auth_status", AUTH_PASS) == AUTH_PASS
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
