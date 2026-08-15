"""scripts/fixture_privacy_scan.py -- automated fixture privacy/secret
scan (security-remediation unit, 2026-08-15; LOW finding, Codex Security
scan: "real broker captures tracked as fixtures").

Two kinds of tests here, deliberately:
  1. Detection-logic tests against SYNTHETIC, obviously-fake secret-shaped
     strings in a tmp_path -- proves the scanner actually catches what it
     claims to, without needing (or risking committing) a real secret.
  2. One integration test that runs the REAL scanner against the REAL
     `scripts/fixtures/` directory in this repo -- the "automated" half of
     the finding's own instruction: this runs on every normal test-suite
     invocation, so a future capture that leaks a credential fails CI
     immediately, not just an operator's memory to re-run this by hand.

No test in this file ever asserts on, or prints, the CONTENT of a real
fixture file -- only `scan_directory(...)`'s own structured findings
(file/location/pattern-name), which never includes a matched value; see
scripts/fixture_privacy_scan.py's own module docstring, "NEVER PRINTS A
MATCHED VALUE" section.
"""
from __future__ import annotations

import json
from pathlib import Path

from scripts.fixture_privacy_scan import (CREDENTIAL_LIKE_KEYS, main,
                                          scan_directory, scan_file)

_REPO_ROOT = Path(__file__).resolve().parent.parent


# ------------------------------------------------------- clean fixtures

def test_a_directory_with_no_secrets_scans_clean(tmp_path):
    (tmp_path / "account.json").write_text(json.dumps({
        "body": {"cash": "500", "equity": "500.12",
                 "apca-api-secret-key": "***REDACTED***"},
    }))
    assert scan_directory(tmp_path) == []


def test_a_nonexistent_directory_scans_clean_not_an_error(tmp_path):
    assert scan_directory(tmp_path / "does-not-exist") == []


def test_a_bare_account_uuid_or_account_number_is_not_flagged():
    """The load-bearing scope decision (module docstring): a persistent
    IDENTIFIER (a UUID, or a human account_number like "PA" + digits) is
    NOT a secret, and this scanner must not flag it -- only genuinely
    secret-shaped strings and unredacted credential-shaped KEYS."""
    findings = []
    body = {
        "body": {
            "id": "00000000-0000-4000-8000-000000000001",
            "account_number": "PA00SYNTHETIC1",
            "client_order_id": "00000000-0000-4000-8000-000000000002",
        },
    }
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "synthetic.json"
        p.write_text(json.dumps(body))
        findings = scan_directory(Path(d))
    assert findings == []


# ------------------------------------------------- unredacted credential key

def test_an_unredacted_value_under_a_credential_shaped_key_is_flagged(tmp_path):
    (tmp_path / "leaky.json").write_text(json.dumps({
        "body": {"apca-api-secret-key": "not-actually-redacted-oops-12345"},
    }))
    findings = scan_directory(tmp_path)
    # The structured JSON-key walk and the regex text scan can both catch
    # the same leak (deliberate defense-in-depth, not a bug) -- what
    # matters is that the structured check fired with the right location.
    key_walk_findings = [f for f in findings if f.pattern_name == "unredacted_credential_key"]
    assert len(key_walk_findings) == 1
    assert key_walk_findings[0].location == "$.body.apca-api-secret-key"


def test_every_credential_like_key_name_is_individually_detected(tmp_path):
    for key in CREDENTIAL_LIKE_KEYS:
        p = tmp_path / f"{key.replace('-', '_')}.json"
        p.write_text(json.dumps({key: "sk-obviously-fake-value-1234567890"}))
    findings = scan_directory(tmp_path)
    flagged_keys = {f.location.split(".")[-1] for f in findings
                    if f.pattern_name == "unredacted_credential_key"}
    assert flagged_keys == CREDENTIAL_LIKE_KEYS


def test_the_redaction_placeholder_itself_is_never_flagged(tmp_path):
    (tmp_path / "properly_redacted.json").write_text(json.dumps({
        "body": {"secret": "***REDACTED***", "token": "***REDACTED***"},
    }))
    assert scan_directory(tmp_path) == []


def test_a_null_or_empty_credential_value_is_not_flagged(tmp_path):
    """A key that is genuinely absent-or-blank (never populated at all,
    e.g. an unauthenticated capture path) is a different, non-leaking
    case from "redacted after being real" -- not a finding either way."""
    (tmp_path / "blank.json").write_text(json.dumps({
        "body": {"password": None, "token": ""},
    }))
    assert scan_directory(tmp_path) == []


# --------------------------------------------------- secret-shaped patterns

def test_an_alpaca_style_live_key_id_is_flagged(tmp_path):
    (tmp_path / "leak.txt").write_text("stray debug line: AK1234567890ABCDEF\n")
    findings = scan_directory(tmp_path)
    assert any(f.pattern_name == "alpaca_live_key_id" for f in findings)


def test_an_alpaca_style_paper_key_id_is_flagged(tmp_path):
    (tmp_path / "leak.txt").write_text("stray debug line: PK1234567890ABCDEF\n")
    findings = scan_directory(tmp_path)
    assert any(f.pattern_name == "alpaca_paper_key_id" for f in findings)


def test_an_aws_style_access_key_is_flagged(tmp_path):
    (tmp_path / "leak.txt").write_text("AKIAABCDEFGHIJKLMNOP\n")
    findings = scan_directory(tmp_path)
    assert any(f.pattern_name == "aws_access_key_id" for f in findings)


def test_a_private_key_pem_header_is_flagged(tmp_path):
    (tmp_path / "leak.txt").write_text("-----BEGIN RSA PRIVATE KEY-----\n")
    findings = scan_directory(tmp_path)
    assert any(f.pattern_name == "private_key_pem_header" for f in findings)


def test_a_bearer_token_is_flagged(tmp_path):
    (tmp_path / "leak.txt").write_text("Authorization: Bearer abcdefghijklmnopqrstuvwxyz123456\n")
    findings = scan_directory(tmp_path)
    assert any(f.pattern_name == "generic_bearer_token" for f in findings)


def test_an_openai_style_secret_is_flagged(tmp_path):
    (tmp_path / "leak.txt").write_text("sk-abcdefghijklmnopqrstuvwxyz123456\n")
    findings = scan_directory(tmp_path)
    assert any(f.pattern_name == "openai_style_secret" for f in findings)


def test_a_slack_style_token_is_flagged(tmp_path):
    (tmp_path / "leak.txt").write_text("xoxb-1234567890-abcdefghij\n")
    findings = scan_directory(tmp_path)
    assert any(f.pattern_name == "slack_token" for f in findings)


# ------------------------------------------------ never prints a matched value

def test_a_finding_description_never_contains_the_actual_matched_secret(tmp_path):
    secret_value = "AK9999999999999999SUPERSECRET"
    (tmp_path / "leak.txt").write_text(f"key: {secret_value}\n")
    findings = scan_directory(tmp_path)
    assert findings
    for f in findings:
        assert secret_value not in f.describe(relative_to=tmp_path)


# --------------------------------------------------------------- non-scanned

def test_files_outside_the_scanned_suffix_set_are_ignored(tmp_path):
    (tmp_path / "leak.bin").write_bytes(b"AK1234567890ABCDEF")
    assert scan_directory(tmp_path) == []


def test_a_malformed_json_file_still_gets_text_pattern_scanning(tmp_path):
    """`scan_file` must not raise or silently skip the whole file just
    because it fails to parse as JSON -- the regex-based text scan below
    still runs."""
    (tmp_path / "broken.json").write_text("{not: valid json, AK1234567890ABCDEF")
    findings = scan_directory(tmp_path)
    assert any(f.pattern_name == "alpaca_live_key_id" for f in findings)


# --------------------------------------------------------- CLI + integration

def test_cli_exits_zero_on_a_clean_directory(tmp_path):
    (tmp_path / "clean.json").write_text(json.dumps({"a": 1}))
    assert main(["--target", str(tmp_path)]) == 0


def test_cli_exits_one_on_a_dirty_directory(tmp_path):
    (tmp_path / "leak.txt").write_text("AK1234567890ABCDEF\n")
    assert main(["--target", str(tmp_path)]) == 1


def test_the_real_committed_scripts_fixtures_directory_scans_clean():
    """THE load-bearing, automated check the finding's own instruction
    named: runs on every normal test-suite invocation against the REAL
    `scripts/fixtures/` directory. If a future capture (or a hand-edit)
    ever introduces a genuine credential-shaped leak, this test -- not
    just scripts/fixture_privacy_scan.py's own standalone CLI -- fails
    immediately."""
    findings = scan_directory(_REPO_ROOT / "scripts" / "fixtures")
    assert findings == [], (
        f"{len(findings)} possible secret(s) found in scripts/fixtures/ -- "
        "run `python3 scripts/fixture_privacy_scan.py` locally for details "
        "(never printed here, per this scanner's own no-secret-values-in-"
        "output rule)"
    )
