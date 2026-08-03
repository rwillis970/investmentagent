"""The launchd-deploy-broken follow-up's own regression test (2026-08-03):
the checked-in `deploy/com.investmentagent.reconcile-loop.plist`'s
`ProgramArguments`, fed through `scripts/run_agent.py`'s REAL argument
parser (`_parse_args`), must actually parse. This is the exact test that
would have caught the original defect: the wiring unit added six (in fact,
by the time every prior gap was counted, eleven) required, no-default
store-path flags to the parser, but the checked-in plist template and
deploy/README.md were never updated to match, so the real, running
launchd job failed argparse on every restart and crash-looped.

VERIFIED THIS TEST'S OWN DISCRIMINATING POWER DIRECTLY, not just asserted
it: loaded the git-HEAD (pre-this-unit) versions of both
`scripts/run_agent.py` and this plist via `git show HEAD:...`, substituted
this same test's placeholder values into the pre-fix template's
`ProgramArguments`, and ran them through the pre-fix `_parse_args`.  That
combination raised `SystemExit(2)`, citing exactly the six flags the
wiring unit had added with no default (`--cash-quarantine-store-path`,
`--fact-store-path`, `--cost-ledger-path`, `--extraction-cache-path`,
`--analysis-result-store-path`, `--opportunity-tracker-path`) -- the real,
reproduced crash-loop. This file tests the CURRENT (fixed) parser against
the CURRENT (fixed) template, which must now succeed; the historical
failure is not re-encoded as an automated test here (that would mean
depending on git history from a live test, which is its own bad idea) but
is recorded in this unit's own report.
"""
from __future__ import annotations

import plistlib
from pathlib import Path

from scripts.run_agent import _parse_args

PLIST_PATH = (Path(__file__).resolve().parent.parent
             / "deploy" / "com.investmentagent.reconcile-loop.plist")

_PLACEHOLDER_SUBSTITUTIONS = {
    "REPLACE_WITH_ACCOUNT_ID": "acct-real",
    "REPLACE_WITH_ALPACA_PAPER_KEY_ID": "key-real",
    "REPLACE_WITH_KEYCHAIN_SECRET_REF": "alpaca_secret_key",
}


def _substituted_argv(tmp_path: Path) -> list[str]:
    with PLIST_PATH.open("rb") as fh:
        plist = plistlib.load(fh)
    raw_args = plist["ProgramArguments"][2:]   # skip python3 + the script path itself
    out = []
    for arg in raw_args:
        if arg in _PLACEHOLDER_SUBSTITUTIONS:
            out.append(_PLACEHOLDER_SUBSTITUTIONS[arg])
        elif arg.startswith("/REPLACE/WITH/ABSOLUTE/PATH/TO"):
            out.append(arg.replace("/REPLACE/WITH/ABSOLUTE/PATH/TO", str(tmp_path)))
        else:
            out.append(arg)
    return out


def test_the_checked_in_templates_program_arguments_parse_against_the_real_parser(tmp_path):
    """The regression test itself: substitute the placeholders with real
    tmp_path-based values and feed the result through the real
    `_parse_args`. Must not raise `SystemExit` -- a template/parser
    mismatch (a required flag the template forgot) is exactly what this
    guards against."""
    argv = _substituted_argv(tmp_path)
    args = _parse_args(argv)   # raises SystemExit(2) on any mismatch
    assert args.config.endswith("config.json")
    assert args.account_id == "acct-real"
    assert args.key_id == "key-real"
    assert args.secret_ref == "alpaca_secret_key"
    assert args.account_type == "TAXABLE"


def test_the_templates_data_dir_is_substituted_to_an_absolute_tmp_path(tmp_path):
    argv = _substituted_argv(tmp_path)
    args = _parse_args(argv)
    assert Path(args.data_dir).is_absolute()
    assert args.data_dir == str(tmp_path / "state")


def test_every_store_path_defaults_correctly_from_the_templates_data_dir(tmp_path):
    """Confirms the template's single --data-dir flag is actually doing the
    job the eleven individual flags used to -- every store lands under it,
    with the exact filenames scripts/run_agent.py's own
    _DEFAULT_STORE_FILENAMES table promises."""
    argv = _substituted_argv(tmp_path)
    args = _parse_args(argv)
    state_dir = tmp_path / "state"
    assert args.ledger_store_path == str(state_dir / "ledger.jsonl")
    assert args.quarantine_store_path == str(state_dir / "quarantine.jsonl")
    assert args.cash_quarantine_store_path == str(state_dir / "cash_quarantine.jsonl")
    assert args.fact_store_path == str(state_dir / "facts.jsonl")
    assert args.cost_ledger_path == str(state_dir / "cost_ledger.jsonl")
    assert args.extraction_cache_path == str(state_dir / "extraction_cache.jsonl")
    assert args.analysis_result_store_path == str(state_dir / "analysis_results.jsonl")
    assert args.approval_request_store_path == str(state_dir / "approval_requests.jsonl")
    assert args.opportunity_tracker_path == str(state_dir / "opportunity_events.jsonl")
    assert args.mode_store_path == str(state_dir / "mode_state.jsonl")
    assert args.audit_log_path == str(state_dir / "audit.jsonl")
