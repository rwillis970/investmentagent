#!/usr/bin/env python3
"""Automated fixture privacy/secret scan (security-remediation unit,
2026-08-15; LOW finding, Codex Security scan: "real broker captures
tracked as fixtures -- do not delete evidence blindly; determine if they
contain persistent account/order/activity identifiers; if so ... add an
automated fixture privacy/secret scan").

WHAT THIS GUARDS AGAINST. `scripts/fixtures/` holds REAL captures taken
against a real (paper) Alpaca account via `scripts/alpaca_probe.py` (see
that script's own `_redact` function and `_CREDENTIAL_LIKE_KEYS` set,
reused here as `CREDENTIAL_LIKE_KEYS`). That redaction already blanks any
JSON value under a credential-shaped KEY at capture time. This scanner is
the independent, second check: it runs over whatever is ALREADY on disk
under a target directory (by default `scripts/fixtures/`) and fails loudly
if either (a) a credential-shaped key somehow still holds a real value
instead of the `***REDACTED***` placeholder -- proving `_redact` actually
ran, not just that it exists -- or (b) any file, regardless of key names,
contains a string matching a known SECRET shape (an Alpaca-style API key
id, an AWS access key, a private-key PEM header, a bearer token, ...).
This is deliberately NOT a scan for persistent but non-secret identifiers
(account UUIDs, order ids, account_numbers) -- those remain in the real
fixture files by design (this codebase's own "do not delete evidence
blindly" posture; see scripts/fixtures/README.md's "SYNTHETIC TEST DATA"
section for the separate remediation that stopped TESTS from reading or
asserting on those specific real values). A CREDENTIAL is categorically
different from an IDENTIFIER: only the former can be used to authenticate
as the account; this scanner exists to make sure the former can never be
one.

NEVER PRINTS A MATCHED VALUE. Every finding this module produces names the
file, the line/JSON-path, and the PATTERN that matched -- never the
matched substring itself. See `Finding.describe()`, the only formatting
path `main()` uses.

RUN IT: `python3 scripts/fixture_privacy_scan.py` (defaults to
`scripts/fixtures/`, recursively) -- exits 0 if clean, 1 if anything was
found. `tests/test_fixture_privacy_scan.py` also runs this against the
REAL `scripts/fixtures/` directory as part of the normal test suite, so a
future capture that leaks a credential fails CI immediately, not just an
operator's memory to re-run this by hand."""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

_DEFAULT_TARGET = _REPO_ROOT / "scripts" / "fixtures"

# Mirrors scripts/alpaca_probe.py's own `_CREDENTIAL_LIKE_KEYS` exactly --
# see that module's own `_redact` for the capture-time half of this
# defense; this is the independent, at-rest half.
CREDENTIAL_LIKE_KEYS = {
    "secret", "api_key", "apca-api-key-id", "apca-api-secret-key",
    "password", "token", "secret_key", "access_token",
}

_REDACTED_PLACEHOLDER = "***REDACTED***"

# (pattern_name, compiled_regex) -- SECRET-shaped strings, not identifier-
# shaped ones. Deliberately does not match a bare UUID (account/order ids
# are UUIDs too, and are NOT secrets -- see module docstring) or a plain
# account-number-looking string (e.g. "PA" + 10 digits).
_SECRET_PATTERNS: tuple[tuple[str, re.Pattern], ...] = (
    ("alpaca_live_key_id", re.compile(r"\bAK[A-Z0-9]{16,}\b")),
    ("alpaca_paper_key_id", re.compile(r"\bPK[A-Z0-9]{16,}\b")),
    ("aws_access_key_id", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("private_key_pem_header", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("generic_bearer_token", re.compile(r"\bBearer [A-Za-z0-9\-_.]{20,}\b")),
    ("openai_style_secret", re.compile(r"\bsk-[A-Za-z0-9]{20,}\b")),
    ("slack_token", re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}\b")),
    # A long, unbroken base64/hex-looking run assigned right after a
    # colon+quote following one of the credential-like key names, missed
    # by the structured JSON-key walk below (e.g. inside a non-JSON file,
    # or a JSON value that itself contains embedded key=value text).
    ("inline_secret_assignment",
     re.compile(r'(?i)\b(?:' + "|".join(re.escape(k) for k in CREDENTIAL_LIKE_KEYS)
                + r')["\']?\s*[:=]\s*["\']?(?!\*\*\*REDACTED\*\*\*)[A-Za-z0-9/+_\-]{16,}')),
)

_SCANNED_SUFFIXES = {".json", ".htm", ".html", ".txt", ".md"}


@dataclass(frozen=True)
class Finding:
    file: Path
    location: str   # a JSON path like "$.body.api_key", or "line 42"
    pattern_name: str

    def describe(self, *, relative_to: Path) -> str:
        try:
            shown = self.file.relative_to(relative_to)
        except ValueError:
            shown = self.file
        return f"{shown} ({self.location}): possible {self.pattern_name} -- value withheld"


def _walk_json_for_credential_keys(obj: Any, *, path: str) -> list[tuple[str, Any]]:
    """Returns (json_path, value) pairs for every key that looks
    credential-shaped and whose value is NOT the redaction placeholder (or
    empty/None -- an intentionally-blanked-a-different-way value is not a
    finding)."""
    hits: list[tuple[str, Any]] = []
    if isinstance(obj, dict):
        for key, value in obj.items():
            child_path = f"{path}.{key}"
            if (key.lower() in CREDENTIAL_LIKE_KEYS
                    and value not in (None, "", _REDACTED_PLACEHOLDER)):
                hits.append((child_path, value))
            hits.extend(_walk_json_for_credential_keys(value, path=child_path))
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            hits.extend(_walk_json_for_credential_keys(item, path=f"{path}[{i}]"))
    return hits


def _scan_text_for_secret_patterns(text: str) -> list[tuple[str, str]]:
    """Returns (pattern_name, 'line N') pairs -- never the matched
    substring itself."""
    hits: list[tuple[str, str]] = []
    for line_no, line in enumerate(text.splitlines(), start=1):
        for pattern_name, pattern in _SECRET_PATTERNS:
            if pattern.search(line):
                hits.append((pattern_name, f"line {line_no}"))
    return hits


def scan_file(path: Path) -> list[Finding]:
    findings: list[Finding] = []
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return findings

    if path.suffix == ".json":
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            data = None
        if data is not None:
            for json_path, _value in _walk_json_for_credential_keys(data, path="$"):
                findings.append(Finding(file=path, location=json_path,
                                        pattern_name="unredacted_credential_key"))

    for pattern_name, location in _scan_text_for_secret_patterns(text):
        findings.append(Finding(file=path, location=location, pattern_name=pattern_name))

    return findings


def scan_directory(target: Path) -> list[Finding]:
    """Recursively scans every file under `target` whose suffix is one of
    `_SCANNED_SUFFIXES`. Returns an empty list for a directory that does
    not exist (nothing to scan is not a failure -- mirrors this
    codebase's own NOT_YET_OBSERVED posture elsewhere, e.g. agent.
    diagnostics)."""
    target = Path(target)
    if not target.is_dir():
        return []
    findings: list[Finding] = []
    for path in sorted(target.rglob("*")):
        if path.is_file() and path.suffix in _SCANNED_SUFFIXES:
            findings.extend(scan_file(path))
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--target", default=str(_DEFAULT_TARGET),
                        help=f"directory to scan recursively (default: {_DEFAULT_TARGET})")
    args = parser.parse_args(argv)

    target = Path(args.target)
    findings = scan_directory(target)
    if findings:
        print(f"FIXTURE PRIVACY SCAN FAILED: {len(findings)} possible secret(s) "
             f"found under {target}", file=sys.stderr)
        for finding in findings:
            print(f"  - {finding.describe(relative_to=_REPO_ROOT)}", file=sys.stderr)
        return 1
    print(f"fixture privacy scan OK: no possible secrets found under {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
