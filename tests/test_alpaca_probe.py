"""scripts/alpaca_probe.py -- a standalone, read-only operator tool, NOT
part of the runtime package.

Three things are worth automated testing here: the pure logic (`_redact`,
`_get`), the orchestration in `probe()` (now injectable: `transport` and
`secrets_provider` are optional constructor-style arguments, the same
dependency-injection shape `AlpacaPaperAdapter` uses, added specifically so
this doesn't require a real account or real credentials to test), and the
structural (not just documentary) guarantee that this script has no write
code path at all. `main()`'s default wiring (real `KeychainSecretsProvider`,
real `UrllibTransport`) is NOT exercised by any test here -- that still
requires a real Alpaca paper account, which this test environment does not
have. See the delivery report for why the capture-and-answer half of this
unit's predecessor could not be completed end-to-end from here.
"""
from __future__ import annotations

import ast
import json
from pathlib import Path

from agent.broker.transport import ScriptedTransport
from agent.secrets_provider import InMemorySecretsProvider
from scripts.alpaca_probe import DEFAULT_SYMBOLS, _get, _redact, probe

SCRIPT_PATH = Path(__file__).resolve().parent.parent / "scripts" / "alpaca_probe.py"


def secrets_provider(secret_ref="alpaca-secret", value="s3cr3t"):
    p = InMemorySecretsProvider(mode="PAPER")
    p.put(secret_ref, value)
    return p


# --------------------------------------------------------------------- _redact

def test_redact_blanks_credential_shaped_keys():
    data = {"cash": "500.00", "secret": "abc", "nested": {"api_key": "xyz", "ok": 1}}
    redacted = _redact(data)
    assert redacted["cash"] == "500.00"
    assert redacted["secret"] == "***REDACTED***"
    assert redacted["nested"]["api_key"] == "***REDACTED***"
    assert redacted["nested"]["ok"] == 1


def test_redact_is_case_insensitive_on_key_name():
    data = {"Secret": "abc", "APCA-API-SECRET-KEY": "xyz"}
    redacted = _redact(data)
    assert redacted["Secret"] == "***REDACTED***"
    assert redacted["APCA-API-SECRET-KEY"] == "***REDACTED***"


def test_redact_walks_lists_of_dicts():
    data = [{"token": "x", "qty": "1"}, {"token": "y", "qty": "2"}]
    redacted = _redact(data)
    assert redacted[0] == {"token": "***REDACTED***", "qty": "1"}
    assert redacted[1] == {"token": "***REDACTED***", "qty": "2"}


def test_redact_leaves_non_credential_fields_completely_untouched():
    """The instruction was "dump every field verbatim except anything that
    is itself a credential" -- verbatim means verbatim, not reformatted or
    coerced."""
    data = {"cash": "500.00", "multiplier": "1", "pattern_day_trader": False,
           "daytrade_count": 0, "symbol": "SPY"}
    assert _redact(data) == data


# ----------------------------------------------------------------------- _get

def test_get_helper_issues_a_get_and_returns_status_and_body():
    t = ScriptedTransport()
    t.enqueue(200, {"cash": "500.00"})
    status, body = _get(t, {"APCA-API-KEY-ID": "x"}, "/v2/account")
    assert status == 200
    assert body == {"cash": "500.00"}
    call = t.calls[0]
    assert call["method"] == "GET"
    assert call["path"] == "https://paper-api.alpaca.markets/v2/account"


def test_get_helper_forwards_params():
    t = ScriptedTransport()
    t.enqueue(200, [])
    _get(t, {}, "/v2/orders", params={"status": "all"})
    assert t.calls[0]["params"] == {"status": "all"}


# --------------------------------------------------------------- probe()

def _queued_transport(symbols):
    """A ScriptedTransport pre-loaded with one response per endpoint, in the
    exact order `probe()` is expected to call them: the original four,
    then configurations, then one /v2/assets/{symbol} per symbol."""
    t = ScriptedTransport()
    t.enqueue(200, {"cash": "500"})       # account
    t.enqueue(200, [])                    # positions
    t.enqueue(200, [])                    # orders
    t.enqueue(200, [])                    # activities
    t.enqueue(200, {"fractional_trading": True, "no_shorting": False})  # configurations
    for _ in symbols:
        t.enqueue(200, {"fractionable": True, "shortable": True})       # /v2/assets/{symbol}
    return t


def test_probe_hits_account_configurations_endpoint():
    t = _queued_transport(("SPY", "QQQ"))
    probe("AK123", "alpaca-secret", Path("/tmp/unused-probe-out"),
         symbols=("SPY", "QQQ"), transport=t, secrets_provider=secrets_provider())
    paths = [c["path"] for c in t.calls]
    assert "https://paper-api.alpaca.markets/v2/account/configurations" in paths


def test_probe_hits_assets_endpoint_for_every_symbol_in_the_small_set():
    t = _queued_transport(("SPY", "QQQ"))
    probe("AK123", "alpaca-secret", Path("/tmp/unused-probe-out"),
         symbols=("SPY", "QQQ"), transport=t, secrets_provider=secrets_provider())
    paths = [c["path"] for c in t.calls]
    assert "https://paper-api.alpaca.markets/v2/assets/SPY" in paths
    assert "https://paper-api.alpaca.markets/v2/assets/QQQ" in paths


def test_probe_writes_configurations_and_assets_json_files(tmp_path):
    t = _queued_transport(("SPY",))
    probe("AK123", "alpaca-secret", tmp_path, symbols=("SPY",),
         transport=t, secrets_provider=secrets_provider())
    configs = json.loads((tmp_path / "configurations.json").read_text())
    assert configs["body"]["fractional_trading"] is True
    assets = json.loads((tmp_path / "assets.json").read_text())
    assert assets["SPY"]["body"]["fractionable"] is True


def test_probe_manifest_lists_the_new_endpoints(tmp_path):
    t = _queued_transport(("SPY",))
    probe("AK123", "alpaca-secret", tmp_path, symbols=("SPY",),
         transport=t, secrets_provider=secrets_provider())
    manifest = json.loads((tmp_path / "capture_manifest.json").read_text())
    assert "/v2/account/configurations" in manifest["endpoints"]
    assert "/v2/assets/SPY" in manifest["endpoints"]


def test_default_symbols_is_a_small_fixed_set():
    """"a small set of symbols" per the prompt -- not the whole tradable
    universe, and not configurable-by-surprise: a fixed, named default."""
    assert 1 <= len(DEFAULT_SYMBOLS) <= 5
    assert len(DEFAULT_SYMBOLS) == len(set(DEFAULT_SYMBOLS))


# ---------------------------------------------- structural no-write-path proof

def test_probe_script_never_calls_request_with_a_write_verb():
    """Not a comment-level promise: parses the script's own AST and checks
    every call to something named `.request(...)` passes method="GET",
    wherever that's statically determinable. This is the actual mechanism
    that makes "no write code path" true rather than merely documented."""
    tree = ast.parse(SCRIPT_PATH.read_text())
    checked = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and node.func.attr == "request":
            checked += 1
            method_value = None
            if node.args and isinstance(node.args[0], ast.Constant):
                method_value = node.args[0].value
            for kw in node.keywords:
                if kw.arg == "method" and isinstance(kw.value, ast.Constant):
                    method_value = kw.value.value
            assert method_value == "GET", (
                f".request(...) call at line {node.lineno} uses "
                f"method={method_value!r}, not 'GET'"
            )
    assert checked >= 1, "expected at least one .request(...) call to check"


def test_probe_script_source_contains_no_write_verb_string_literals_outside_the_docstring():
    """A second, independent check: no string literal anywhere in the
    module equals a write HTTP verb, except inside the module docstring
    itself (which discusses them in prose, explaining their absence)."""
    tree = ast.parse(SCRIPT_PATH.read_text())
    doc_node = tree.body[0] if tree.body and isinstance(tree.body[0], ast.Expr) else None
    forbidden = {"POST", "PUT", "PATCH", "DELETE"}
    offenders = []
    for node in ast.walk(tree):
        if node is (doc_node.value if doc_node else None):
            continue
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value in forbidden:
            offenders.append((node.lineno, node.value))
    assert offenders == [], f"write-verb string literal(s) found in code: {offenders}"


def test_probe_script_has_no_import_of_anything_from_the_runtime_startup_or_pipeline():
    """This is a one-off operator tool, not part of the runtime package --
    it must not import agent.startup, agent.pipeline's Gatekeeper, or
    anything that could stage or submit an order. It only needs read
    access: credentials, the transport, and the accounts module for typing."""
    tree = ast.parse(SCRIPT_PATH.read_text())
    imported_modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported_modules.add(node.module)
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported_modules.add(alias.name)
    forbidden_modules = {"agent.startup", "agent.pipeline"}
    assert not (imported_modules & forbidden_modules), (
        f"probe script imports from a write-capable module: "
        f"{imported_modules & forbidden_modules}"
    )
