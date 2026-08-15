# Admin Console V1 implementation

## Revision and scope

- Main/base HEAD: `8a260d996636c73dbd4ac540bd6edc8fccc094f5`
- Feature implementation HEAD: `5043190890ac3ce43037b38b76190b1636acd044`
- Branch: `codex/admin-console-v1`
- Required-fix scope: CSRF protection, evidence CLI correction, high-value
  regression coverage, status UX hardening, and this regenerated report.
- Phase 2/3 overlap: none. No trading, approval, mode, broker, collection,
  materiality, pipeline, acceptance, or research-once implementation changed.

The required-fix commit changes `agent/admin_console.py`, the three admin
static assets, `tests/test_admin_console.py`, and adds
`tests/test_admin_console_bind.js`. This report is the only subsequent
documentation-only change.

## Architecture and status sources

The console remains a small `ThreadingHTTPServer` with pure route dispatch and
static HTML/CSS/JavaScript. `AdminRuntime` injects paths and a service manager.
Production uses `LaunchctlServiceManager`; tests use a fake and mock every
`launchctl` subprocess boundary.

- Service state: `launchctl list` for exactly
  `com.investmentagent.reconcile-loop` and
  `com.investmentagent.dashboard`.
- Broker environment: deployed `config.json`, with only explicit Alpaca
  paper/live values mapped.
- Operational state: read-only `ModeStore.current()`.
- Runtime and reconciliation: `runtime_status.json`, including the shared
  staleness rule.
- Failure state: authoritative `failure_sentinel.json` loader, representing
  `ACTIVE`, `RECOVERED`, `NONE`, or `UNAVAILABLE` without invention.
- Backup: newest existing `backups/*/manifest.json`.
- Dashboard URL: installed dashboard plist first, checked-in plist second;
  only an explicit loopback host and parsed port are accepted.
- Git state: branch, HEAD, cleanliness, and `git ls-files -- data data/**`.
  Any tracked runtime data is a prominent `FAIL`.
- Cash and positions: `UNAVAILABLE`, because the persisted runtime snapshot
  does not contain an authoritative value and this console never contacts a
  broker.

The evidence utility now uses the real nested CLI shape:

```text
inspect_evidence.py --data-dir <disposable-data> facts list --limit 50
```

An end-to-end test invokes that real script and parser against a disposable
directory without mocking its CLI.

## CSRF design and endpoint protection

`AdminRuntime` generates a 256-bit-equivalent URL-safe token with
`secrets.token_urlsafe(32)` once per process. The token is held only in memory,
excluded from dataclass representations, never put in a URL, never persisted,
and never logged. It is HTML-escaped into a meta element in the locally served
index page; the dynamic page is returned with `Cache-Control: no-store`.

The frontend reads that same-origin meta value and sends it in
`X-InvestmentAgent-CSRF`. The shared dispatcher uses constant-time comparison
and returns 403 before either state-changing sink when the header is missing or
wrong. Protection covers every `/api/services/*` and `/api/utilities/*` POST.
There is no permissive CORS header and no `OPTIONS` implementation granting
cross-origin access, so a hostile webpage cannot send the required custom
header via a blind simple request. GET status, logs, and static assets remain
usable without a token.

The console still has no endpoints for submitting/cancelling orders,
approvals/tokens, operational-mode changes, production activation,
credentials/secrets, ledger or cash repair, or quarantine mutation. Service
labels and actions are exact allowlists; adversarial separators, substitution
syntax, traversal fragments, whitespace, and newlines are rejected.

## Status UX

`STALE` and `NOT_YET_OBSERVED` render amber; `UNAVAILABLE` and `UNKNOWN` render
muted gray; failures/active incidents/stopped services render red. These states
are no longer visually equivalent to confirmed-good green values, and the UI
does not invent replacement status.

## Launch/reboot and exact commands

The LaunchAgent binds `127.0.0.1:8766`, uses `RunAtLoad`, failed-exit
keepalive, and a 60-second throttle. The installer renders and writes only this
plist and does not invoke `launchctl` or change either trading service.

```sh
cd /Users/raywillis/projects/investmentagent
/usr/bin/python3 scripts/install_admin_console.py --data-dir /Users/raywillis/projects/investmentagent/data --backup-dir /Users/raywillis/projects/investmentagent/backups --log-dir /Users/raywillis/projects/investmentagent/logs
launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/com.investmentagent.admin-console.plist"
```

```sh
launchctl bootout "gui/$(id -u)/com.investmentagent.admin-console"
cd /Users/raywillis/projects/investmentagent
/usr/bin/python3 scripts/uninstall_admin_console.py
```

Exact URL: `http://127.0.0.1:8766`

## Fresh verification results

- Focused Python: 65 passed, 0 failed.
- Full Python on disposable test data using supported Python 3.12 and a
  disposable PATH without real `launchctl`: 5,242 passed, 0 failed; 9 existing
  Python 3.14 tar-extraction deprecation warnings.
- JavaScript: 58 passed, 0 failed.
- Python compilation, admin JavaScript syntax, and `git diff --check`: pass.
- No test accessed canonical runtime data and no test or development command
  invoked real `launchctl`.

## Static safety results

- Production `adapter.submit(...)` call count: 1, unchanged, at
  `agent/approval_execution.py:451`.
- `adapter.cancel(...)` call count: 0.
- New approval paths: 0.
- New mode-writing paths: 0.
- Secret-return endpoints: 0; the CSRF value is an ephemeral anti-forgery
  nonce, not a credential or runtime secret, and is exposed only in same-origin
  HTML for this process.
- `t4_analysis_enabled`: `false` in `config.example.json` and `False` by
  default in `agent/config.py`.
- `materiality_threshold`: 2.0 in both authoritative defaults.
- Capability widening: none; no capability, broker, trading, policy, or config
  implementation file changed.
- `shell=True` occurrences under `agent/` and `scripts/`: 0.
- Git-tracked `data/` paths: 0.

## Known limitations

Cash and positions await an authoritative persisted read model. Utility
execution remains synchronous and output is bounded to 12,000 characters.
The console has no user authentication and must remain loopback-only. Anyone
with local process/browser control at the operator's account privilege level is
outside the CSRF threat boundary. Bootstrap and bootout remain explicit
operator actions.

ADMIN CONSOLE V1 READY FOR CROSS-REVIEW: YES
