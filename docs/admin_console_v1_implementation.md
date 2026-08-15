# Admin Console V1 implementation

## Revision and scope

- Starting HEAD: `3ba04707905d3b0d41e1fd49ad33565106411f07`
- Ending HEAD / commit: recorded in the final handoff (a commit cannot contain its own hash).
- Branch: `codex/admin-console-v1`
- Phase 2/3 overlap: none; collection, materiality, pipeline, acceptance, and research-once files were not edited.

Added: `agent/admin_console.py`, runner, admin-only install/uninstall scripts,
static UI, LaunchAgent template, focused tests, and this report. Modified:
`deploy/README.md` only.

## Architecture and source mapping

This is a small `ThreadingHTTPServer` with pure routing and static HTML/CSS/JS.
`AdminRuntime` injects all paths and a service manager. Production uses
`LaunchctlServiceManager`; tests use a fake. Service state comes from
`launchctl list` for exactly two allowlisted labels. Broker environment comes
from deployed `config.json`; operational state from `ModeStore`; runtime and
reconciliation from `runtime_status.json` and its shared staleness rule;
failure state from `failure_sentinel.json`; backup visibility from the newest
`backups/*/manifest.json`. Cash and positions honestly remain `UNAVAILABLE`
because the persisted runtime snapshot does not contain authoritative values
and this console never contacts a broker.

Dashboard URL discovery reads the installed dashboard plist first and the
checked-in plist second, accepting only an explicit loopback host and parsed
port. Git state reports branch, HEAD, and porcelain cleanliness. Runtime-data
protection runs `git ls-files -- data data/**`; any tracked result is `FAIL`.

Existing health, pre-reboot, evidence, and backup scripts are invoked using
fixed argument arrays. Log viewing is restricted to bounded tails of six known
log files. Unknown utilities remain unavailable.

## Security boundaries

Binding rejects every host except `127.0.0.1`, `localhost`, and `::1`; the
LaunchAgent pins `127.0.0.1`. Labels and actions are strict allowlists. There
are no order, cancellation, approval, mode-change, credential, secret, ledger,
or quarantine endpoints. No broker or secrets module is imported. Unavailable
and stale evidence is never converted to green.

Credentials are deliberately absent in V1. A future page may show configured
state or a masked identifier and allow replacement/validation, but must never
retrieve an existing secret. Account state stays private and isolated; shared,
versioned agent intelligence remains a documented future principle, not a
multi-tenant implementation.

## Launch/reboot behavior and exact commands

The plist uses `RunAtLoad`, failed-exit keepalive, and a 60-second throttle.
The installer renders and atomically writes only this plist; it does not load,
restart, or stop any service.

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

## Tests and safety scans

- Focused Python: 9 passed.
- Full Python: 5,154 discovered; approved real-Mac run had 5,150 pass and four
  pre-existing environment-sensitive failures (two Python 3.9 timeout
  normalization tests although the project requires 3.11+, and two assertions
  explicitly expecting a Linux host without `launchctl`). No admin test failed.
- JavaScript: 56 passed, 0 failed.
- Admin JavaScript syntax and `git diff --check`: pass.
- Static diff scan: no new `adapter.submit`, `adapter.cancel`, automatic
  approval, mode advancement, credential retrieval, Claude/T4 enablement,
  materiality threshold change, or capability widening. Relevant core files
  are unchanged.

## Known limitations

Cash and positions await an authoritative persisted read model. Utility output
is synchronous and bounded to 12,000 characters. There is no authentication,
so loopback enforcement is permanent. Bootstrap/bootout remain explicit
operator commands.

ADMIN CONSOLE V1 READY FOR CROSS-REVIEW: YES
