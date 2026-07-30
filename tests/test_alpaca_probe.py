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
from scripts.alpaca_probe import (DEFAULT_ACTIVITIES_SINCE, DEFAULT_SYMBOLS, _fetch_all_activities_since,
                                  _get, _redact, probe)

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

def _queued_transport(symbols, *, activities_since_pages=([],)):
    """A ScriptedTransport pre-loaded with one response per endpoint, in the
    exact order `probe()` is expected to call them: the original four,
    then configurations, then the paginated activities-since sweep (one
    response per page -- a single empty page by default), then one
    /v2/assets/{symbol} per symbol."""
    t = ScriptedTransport()
    t.enqueue(200, {"cash": "500"})       # account
    t.enqueue(200, [])                    # positions
    t.enqueue(200, [])                    # orders
    t.enqueue(200, [])                    # activities (unfiltered, single-shot)
    t.enqueue(200, {"fractional_trading": True, "no_shorting": False})  # configurations
    for page in activities_since_pages:
        t.enqueue(200, page)             # /v2/account/activities?after=...&direction=asc (paginated)
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


# ---------------------------------- _fetch_all_activities_since (2026-07-30)
# The settled-cash-halt investigation: every Account Activity since the
# Decimal-migration fix date, every type, not just FILL. Confirmed directly
# against docs.alpaca.markets/reference/getaccountactivities-2 (fetched
# 2026-07-30): `after`/`direction`/`page_size`/`page_token` are real query
# params on this exact endpoint (paper-api.alpaca.markets/v2/account/
# activities) -- the same pagination contract `AlpacaPaperAdapter.fills()`
# already relies on for `/v2/account/activities/FILL` (agent/broker/
# alpaca.py), now reused here for the unfiltered endpoint.

def test_fetch_activities_since_sends_after_and_ascending_direction_no_type_filter():
    """No `activity_types`/`category` param is ever sent -- filtering by
    type before knowing what happened is exactly the mistake this capture
    exists to avoid (module docstring: "do not invent the output")."""
    t = ScriptedTransport()
    t.enqueue(200, [])
    _fetch_all_activities_since(t, {"APCA-API-KEY-ID": "x"}, after="2026-07-28")
    call = t.calls[0]
    assert call["path"] == "https://paper-api.alpaca.markets/v2/account/activities"
    assert call["params"]["after"] == "2026-07-28"
    assert call["params"]["direction"] == "asc"
    assert "activity_types" not in call["params"]
    assert "category" not in call["params"]


def test_fetch_activities_since_returns_a_single_short_page_unpaginated():
    t = ScriptedTransport()
    t.enqueue(200, [{"id": "a1", "activity_type": "FILL"}])
    result = _fetch_all_activities_since(t, {}, after="2026-07-28", page_size=100)
    assert result == [{"id": "a1", "activity_type": "FILL"}]
    assert len(t.calls) == 1


def test_fetch_activities_since_paginates_on_a_full_page_using_the_last_ids_page_token():
    """A page exactly `page_size` long is not, by itself, proof there is
    nothing left -- the real signal (per Alpaca's own docs and the already-
    confirmed `fills()` contract) is a page SHORTER than `page_size`. A full
    first page must trigger a second request carrying `page_token` set to
    the first page's own last activity id."""
    t = ScriptedTransport()
    page1 = [{"id": f"a{i}", "activity_type": "FILL"} for i in range(2)]
    page2 = [{"id": "a2", "activity_type": "CSD"}]
    t.enqueue(200, page1)
    t.enqueue(200, page2)
    result = _fetch_all_activities_since(t, {}, after="2026-07-28", page_size=2)
    assert result == page1 + page2
    assert len(t.calls) == 2
    assert t.calls[1]["params"]["page_token"] == "a1"


def test_fetch_activities_since_stops_on_a_page_shorter_than_page_size():
    t = ScriptedTransport()
    t.enqueue(200, [{"id": "a0", "activity_type": "FILL"}])   # 1 < page_size=2 -- done
    result = _fetch_all_activities_since(t, {}, after="2026-07-28", page_size=2)
    assert result == [{"id": "a0", "activity_type": "FILL"}]
    assert len(t.calls) == 1


def test_fetch_activities_since_stops_on_an_empty_page():
    t = ScriptedTransport()
    t.enqueue(200, [])
    result = _fetch_all_activities_since(t, {}, after="2026-07-28")
    assert result == []
    assert len(t.calls) == 1


def test_default_activities_since_is_the_decimal_fix_date():
    """The specific date the Decimal-migration fix (and the settled-cash
    halt this capture investigates) actually happened -- named, not
    guessed, and overridable via --activities-since for future use."""
    assert DEFAULT_ACTIVITIES_SINCE == "2026-07-28"


# --------------------------------- probe() wiring for the activities-since sweep

def test_probe_writes_activities_since_json_preserving_every_activity_type(tmp_path):
    """Non-FILL types (CSD, FEE, ...) must survive verbatim, not be dropped
    or reshaped -- this file exists specifically to show the operator
    exactly what the broker reported, unfiltered."""
    t = _queued_transport(
        ("SPY",),
        activities_since_pages=([
            {"id": "a1", "activity_type": "FILL", "qty": "0.027087234", "price": "737.986"},
            {"id": "a2", "activity_type": "CSD", "net_amount": "0.01"},
            {"id": "a3", "activity_type": "FEE", "net_amount": "-0.01"},
        ],),
    )
    probe("AK123", "alpaca-secret", tmp_path, symbols=("SPY",),
         transport=t, secrets_provider=secrets_provider())
    captured = json.loads((tmp_path / "activities_since.json").read_text())
    assert captured["after"] == DEFAULT_ACTIVITIES_SINCE
    types = {a["activity_type"] for a in captured["activities"]}
    assert types == {"FILL", "CSD", "FEE"}
    assert captured["activity_type_counts"] == {"FILL": 1, "CSD": 1, "FEE": 1}


def test_probe_manifest_lists_the_activities_since_sweep(tmp_path):
    t = _queued_transport(("SPY",), activities_since_pages=([],))
    probe("AK123", "alpaca-secret", tmp_path, symbols=("SPY",),
         transport=t, secrets_provider=secrets_provider())
    manifest = json.loads((tmp_path / "capture_manifest.json").read_text())
    assert any("after=2026-07-28" in e and "direction=asc" in e for e in manifest["endpoints"])


def test_probe_activities_since_is_overridable(tmp_path):
    t = _queued_transport(("SPY",), activities_since_pages=([],))
    probe("AK123", "alpaca-secret", tmp_path, symbols=("SPY",),
         activities_since="2026-01-01", transport=t, secrets_provider=secrets_provider())
    captured = json.loads((tmp_path / "activities_since.json").read_text())
    assert captured["after"] == "2026-01-01"


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
