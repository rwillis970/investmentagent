# Admin Console V1 implementation

## Revision and scope

- Main/base HEAD: `8a260d996636c73dbd4ac540bd6edc8fccc094f5`
- Feature implementation HEAD: `5043190890ac3ce43037b38b76190b1636acd044`
- Security-hardening starting HEAD:
  `006f66d15f7daeab5b963de7993936260cf819f3`.
- Branch: `codex/admin-console-v1`
- Security-fix scope: exact Host and Origin enforcement, CSRF interaction,
  anti-framing headers, bounded HTTP/utility/log resources, Admin utility
  single-flight, disruptive-action confirmation, adversarial coverage, and
  this report.
- Phase 2/3 overlap: none. No trading, approval, mode, broker, collection,
  materiality, pipeline, acceptance, or research-once implementation changed.

The security patch changes `agent/admin_console.py`,
`admin_console/static/app.js`, `tests/test_admin_console.py`,
`tests/test_admin_console_bind.js`, and this report. It does not change
`scripts/backup_snapshot.py` or any trading, approval, broker, mode, or
credential implementation.

## Architecture and status sources

The console remains a small `ThreadingHTTPServer` with pure route dispatch and
static HTML/CSS/JavaScript. It accepts only `127.0.0.1` or `localhost` as bind
arguments and the deployed LaunchAgent still binds `127.0.0.1:8766`.
`AdminRuntime` injects paths and a service manager. Production uses
`LaunchctlServiceManager`; tests use a fake and mock every `launchctl`
subprocess boundary.

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

## Host, Origin, CSRF, and browser framing

`AdminRuntime` generates a 256-bit-equivalent URL-safe token with
`secrets.token_urlsafe(32)` once per process. The token is held only in memory,
excluded from dataclass representations, never put in a URL, never persisted,
and never logged. It is HTML-escaped into a meta element in the locally served
index page; the dynamic page is returned with `Cache-Control: no-store`.

The frontend reads that same-origin meta value and sends it in
`X-InvestmentAgent-CSRF`. Before any route can return HTML, the nonce, static
assets, status, or logs, the dispatcher requires an exact Host value from:

- `127.0.0.1`
- `127.0.0.1:8766`
- `localhost`
- `localhost:8766`

Foreign names, suffixes, alternate ports, missing Host, and duplicate Host are
403. This closes the token-bootstrap step in the DNS-rebinding path.

Every POST must independently pass all three checks before routing: trusted
Host, constant-time CSRF comparison, and exact Origin equal to
`http://127.0.0.1:8766` or `http://localhost:8766`. Missing or foreign Origin is
403 even when the CSRF value is valid. There is no permissive CORS response;
`OPTIONS` is handled as an unsupported route and never grants cross-origin
access. Read-only GETs need no CSRF token but still require trusted Host.

Every routed response, including 403 and 404 responses, carries
`Content-Security-Policy: frame-ancestors 'none'` and
`X-Frame-Options: DENY`; `X-Content-Type-Options: nosniff` is also applied
consistently. The UI asks for explicit confirmation before stop and restart
service actions. Confirmation is defense in depth, not an authorization
boundary.

The console still has no endpoints for submitting/cancelling orders,
approvals/tokens, operational-mode changes, production activation,
credentials/secrets, ledger or cash repair, or quarantine mutation. Service
labels and actions are exact allowlists; adversarial separators, substitution
syntax, traversal fragments, whitespace, and newlines are rejected.

## Resource and backup-collision controls

The HTTP adapter applies a 10-second connection timeout, closes each connection
after one response, rejects all transfer-encoded requests, rejects every
non-empty request body because no V1 route needs one, rejects malformed lengths,
and returns 413 above the 1,024-byte ceiling. Duplicate sensitive headers are
made invalid before routing.

Admin-triggered utilities share one non-blocking process-local gate. If a
backup, pre-reboot, health, or evidence utility is already running, another
Admin utility request returns HTTP 409 with `BUSY` and starts no subprocess.
This prevents overlapping Admin-triggered backups and backup/pre-reboot request
storms. The shared backup implementation remains unchanged and outside this
branch's scope.

Utility subprocesses retain fixed argv construction with no shell. They run in
an isolated process group, receive no stdin, time out after 120 seconds, and
retain only the last 12,000 output bytes while continuously draining the pipe.
Timeout kills the utility process group. Log responses open only six fixed
allowlisted names, refuse symlinks/non-regular files, seek from the end, and
read at most 12,000 bytes rather than loading the whole file.

## Security-finding disposition

- DNS rebinding: **FIXED**. Exact Host precedes every response; exact Origin and
  CSRF precede every POST.
- Clickjacking: **FIXED**. CSP and X-Frame-Options cover every routed response;
  disruptive actions also receive UI confirmation.
- HTTP resource amplification, Admin portion: **FIXED**. Body, connection,
  subprocess, output, log-read, and utility-concurrency bounds are enforced.
- Backup collision, Admin portion: **FIXED**. Overlapping Admin utilities return
  `BUSY`/409; `backup_snapshot.py` was deliberately not changed.
- Local cross-user authority: **ACCEPTED LOCAL LIMITATION** for V1. Host,
  Origin, and CSRF stop hostile webpages/DNS rebinding but cannot authenticate a
  raw local TCP client. Another local UID that can connect to loopback can still
  fetch the nonce and invoke the allowlisted service/utility actions under the
  LaunchAgent's UID. No persisted shared secret was introduced to disguise this
  limitation. V1.1 should use an operator-owned Unix domain socket in a mode
  `0700` directory, socket mode `0600`, and kernel peer-credential validation
  against the operator UID before HTTP dispatch (with a local UI bridge only if
  needed).

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

- Focused Admin Python: 112 passed, 0 failed.
- Full Python on disposable test data using supported Python 3.12 and a
  disposable PATH with no discoverable `launchctl`: 5,289 passed, 0 failed; 9 existing
  Python 3.14 tar-extraction deprecation warnings.
- JavaScript: 59 passed, 0 failed.
- Python compilation, admin JavaScript syntax, and `git diff --check`: pass.
- No test accessed canonical runtime data and no test or development command
  invoked real `launchctl`.

## Static safety results

- Production `adapter.submit(...)` call count: 1, unchanged, at
  `agent/approval_execution.py:451`.
- `adapter.cancel(...)` call count: 0.
- New approval paths in the patch: 0.
- New mode-writing paths in the patch: 0.
- Secret-return endpoints: 0; the CSRF value is an ephemeral anti-forgery
  nonce, not a credential or runtime secret, and is exposed only after exact
  Host validation in process-local HTML.
- `t4_analysis_enabled`: `false` in `config.example.json` and `False` by
  default in `agent/config.py`.
- `materiality_threshold`: 2.0 in both authoritative defaults.
- Capability widening: none; no capability, broker, trading, policy, or config
  implementation file changed.
- `shell=True` occurrences under `agent/` and `scripts/`: 0.
- Git-tracked `data/` paths: 0.

## Known limitations

Cash and positions await an authoritative persisted read model. Utility
execution remains synchronous but is single-flight and bounded. The console
has no OS-user authentication and must remain loopback-only. The cross-UID TCP
authority limitation described above remains open until an authenticated local
transport replaces loopback TCP. Anyone with process control at the operator's
own UID is also outside the CSRF threat boundary. Bootstrap and bootout remain
explicit operator actions.

ADMIN CONSOLE V1 SECURITY HARDENING: COMPLETE WITH ACCEPTED LOCAL LIMITATION
