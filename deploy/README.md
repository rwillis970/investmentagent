# Installing the reconciliation loop (`com.investmentagent.reconcile-loop`)

This installs `scripts/run_agent.py` as a macOS **LaunchAgent** so it runs
continuously, unattended, and restarts itself after a crash.

**Must be a LaunchAgent (`~/Library/LaunchAgents/`), not a LaunchDaemon
(`/Library/LaunchDaemons/`).** Permanent-failure notification (see below)
delivers a desktop notification via `osascript`, which requires a GUI
session context. A LaunchDaemon runs outside any user's GUI session and the
notification would silently go nowhere. This only matters for the
notification path -- the reconciliation loop itself has no other GUI
dependency -- but it is the reason this must load as an agent, in the
logged-in user's own session, not as a daemon at boot.

## 1. Create the state and log directories

```sh
mkdir -p ~/investmentagent/state ~/investmentagent/logs
```

`~/investmentagent/logs` must exist and be writable *before* the job is
loaded -- launchd itself creates `StandardOutPath`/`StandardErrorPath` but
not a missing parent directory for them, and this script has no way to
create that directory on launchd's behalf.

`~/investmentagent/state` (the directory you'll pass as `--data-dir` in
step 2) does NOT strictly need to be pre-created any more: `scripts/
run_agent.py` now creates it itself (`mkdir -p`, equivalent to the command
above) the moment any store path defaults into it. Creating it here
anyway is still recommended -- it lets you confirm the path and
permissions once, up front, rather than discovering a problem on the
first launchd restart. An INDIVIDUAL store path you override explicitly
(rather than leaving to default) is a different matter: for that path,
the underlying store class (`LedgerStore`/`ModeStore`/`AuditLog`/etc.)
still refuses to silently create ITS OWN parent directory, unchanged.

## 2. Fill in the placeholders

Copy `deploy/com.investmentagent.reconcile-loop.plist` to
`~/Library/LaunchAgents/com.investmentagent.reconcile-loop.plist` and
replace every `/REPLACE/...` value:

- the absolute path to `scripts/run_agent.py` in your checkout of this repo
- the absolute path to your `config.json` (see `config.example.json`)
- `--account-id`, `--key-id` for your Alpaca paper account
- `--secret-ref`: the keychain **account name** the API secret is stored
  under (never a raw secret -- see the plist's own top-of-file comment).
  Add it first with `agent.secrets_provider.KeychainSecretsProvider`'s own
  setup, or the macOS `Keychain Access` app / `security add-generic-password`
  directly.
- `--signing-key-secret-ref` (follow-up unit, 2026-08-09): the keychain
  **account name** the durable `agent.pipeline.Gatekeeper` signing key is
  stored under -- a 32+ byte value, hex-encoded, never a raw secret. This
  is what lets a `StagedOrder` signed by the scheduled loop actually verify
  later, when `--submit-approved` runs as a separate process invocation
  (see `agent/approval_execution.py`'s own module docstring for why that
  matters). Provision it once per mode, before loading this job for the
  first time:

  ```
  python3 -c "import secrets; print(secrets.token_bytes(32).hex())"
  security add-generic-password -s investmentagent:PAPER \
      -a <the --signing-key-secret-ref value> -w <the printed hex string>
  ```

  Provisioning a NEW value after orders have already been staged means
  every request staged under the old key can never verify again -- see
  that same module docstring's CUTOVER section for the operator remedy
  (`ApprovalRequestStore.invalidate` on anything still DECIDED but not yet
  submitted; anything still PENDING is unaffected).
- `--data-dir`: an absolute path to the `state/` directory created in
  step 1. Every durable store/log file `scripts/run_agent.py` needs
  (ledger, quarantine, cash-quarantine, fact store, cost ledger,
  extraction cache, analysis-result store, approval-request store,
  opportunity tracker, mode store, audit log -- eleven files in total)
  now defaults to a fixed name inside this one directory; you no longer
  name any of them individually here. See "THE LAUNCHD DEPLOY WAS BROKEN"
  below for why this replaced eleven separate `--..-path` flags.
- `StandardOutPath`/`StandardErrorPath`: paths under the `logs/` directory
  created in step 1

Do not put a raw secret anywhere in the plist itself.

### THE LAUNCHD DEPLOY WAS BROKEN (2026-08-03) -- why this template changed

This template used to enumerate every durable store path as its own
`--..-path` flag. Each time a unit wired a new store into
`scripts/run_agent.py`'s argparse -- most recently the collection/
screening/T4/approval-request pipeline -- that flag had **no default**,
and this checked-in template was never updated to match. The real,
running launchd job failed argparse on every restart as a result and
crash-looped: `KeepAlive` kept relaunching a process that could never get
past its own argument parser. This was the THIRD "wired in tests, absent
in production" defect found in this codebase (`approval_service` being the
second).

The fix is `--data-dir`, not a longer list of `--..-path` flags: every
store/log path `scripts/run_agent.py` needs now defaults to a fixed
filename inside one directory (see that script's own `_DEFAULT_STORE_
FILENAMES` table), so a future store addition cannot reproduce this
defect just by being required with no default. `python3 scripts/
run_agent.py --config config.json --account-id X --key-id Y --secret-ref
Z` -- no path flags at all -- now parses and starts; this template still
pins `--data-dir` to an explicit, absolute path rather than relying on
its own relative default (`./data`), since a relative default resolves
against whatever directory launchd happens to start the process in, which
this template does not otherwise control (no `WorkingDirectory` key is
set). `tests/test_launchd_plist.py` asserts this template never goes back
to enumerating individual store paths, and a separate test
(`tests/test_run_agent_plist_parses.py`) feeds this exact template's
`ProgramArguments` through `scripts/run_agent.py`'s own real argument
parser, so a template/parser mismatch like this one fails a test again
before it ever reaches a running launchd job.

## 3. Advance the persisted mode to PAPER (fresh install only)

A brand-new `state/mode_state.jsonl` starts DISABLED, and §9.2's one-step
rule means PAPER cannot be reached in one step -- and setting
`mode: RESEARCH` (or `PAPER`) in `config.json` and just loading the job
does NOT work either: the loop always constructs a broker adapter bound to
PAPER, which refuses outright if `cfg.mode` isn't PAPER too. Run these two
commands once, from the repo root, before loading the job for the first
time (`--confirmed` is not needed for either step):

```sh
python3 scripts/run_agent.py --data-dir ~/investmentagent/state --advance-mode-to RESEARCH
python3 scripts/run_agent.py --data-dir ~/investmentagent/state --advance-mode-to PAPER
```

(`--data-dir` here resolves to the exact same `mode_state.jsonl`/`audit.jsonl`
paths as before -- those are `_DEFAULT_STORE_FILENAMES`' own names for
`--mode-store-path`/`--audit-log-path`. Pass `--mode-store-path`/
`--audit-log-path` explicitly instead if you ever need either to live
somewhere other than under `--data-dir`.)

Each prints and exits 0 on success (or exits 1 with a clear reason if the
step is illegal or out of order -- nothing is written to either store on a
refusal). Confirm with `--advance-mode-to PAPER` again: a target equal to
the already-persisted mode is a harmless no-op. Make sure `config.json`
itself also has `"mode": "PAPER"` before step 5 -- this only advances the
PERSISTED mode; the loop's own `target_mode` still comes from `config.json`
on every run.

## 4. Preflight the installed copy, then load the job

`tests/test_run_agent_plist_parses.py` only ever validates the CHECKED-IN
TEMPLATE in `deploy/`, before you've filled in a single placeholder --
it has no way to see what you actually typed into your own copy at
`~/Library/LaunchAgents/`. This has already gone wrong once: an installed
copy was missing `--signing-key-secret-ref` entirely and crash-looped on
argparse every `ThrottleInterval` (60s) after `launchctl bootstrap`, with
nothing checking it first. Run this against your OWN installed file, not
the template, before every `bootstrap`:

```sh
python3 deploy/preflight_plist.py ~/Library/LaunchAgents/com.investmentagent.reconcile-loop.plist
```

It checks three things: `ProgramArguments` parses against the real
`scripts/run_agent.py` argument parser (the exact check that would have
caught the missing-flag incident above); every path the plist names --
the script, `--config`, `--data-dir`, and the `StandardOutPath`/
`StandardErrorPath` log directories -- actually exists on this machine;
and no `REPLACE_`/`/REPLACE/...` placeholder was left unfilled anywhere
(a placeholder like `REPLACE_WITH_ACCOUNT_ID` parses fine as a string, so
only this third check catches it). It prints `preflight OK` and exits 0 on
success; on any failure it exits 1 and lists every problem it found, one
per line, each naming the specific flag/key and -- where the plist's own
one-`<string>`-per-line convention makes it unambiguous -- the line number
to fix.

**Does not need your keychain entry provisioned or unlocked first** -- it
never resolves `--secret-ref`/`--signing-key-secret-ref`, only checks that
the flags and values are present and well-formed. A locked keychain is not
a plist problem, and this check does not conflate the two.

## 5. Load the job

```sh
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.investmentagent.reconcile-loop.plist
```

(On older macOS versions without `bootstrap`, use
`launchctl load ~/Library/LaunchAgents/com.investmentagent.reconcile-loop.plist`.)

`RunAtLoad` means it starts immediately; it will also restart automatically
at every login going forward, and after every crash, per `KeepAlive` below.

## What happens on a crash, and how an operator finds out

`agent.run_loop.run_loop`'s own design is: any exception stops the loop and
exits the process non-zero -- it never retries internally. This plist is
the other half of that design: `KeepAlive.SuccessfulExit=false` relaunches
the process on any non-zero exit (never after a clean one, which does not
happen in real unattended operation), throttled by `ThrottleInterval` (60s)
so a persistent failure doesn't spin launchd into a tight restart loop that
burns CPU and hammers the broker's API.

**A transient failure** (a momentary network blip) recovers on its own:
the process relaunches roughly a minute later, the same operation likely
succeeds, and nothing further happens.

**A permanent failure** (a locked keychain, an expired credential, a
genuine reconciliation halt) recurs on every relaunch -- as the SAME
exception TYPE, even if its message's incidental details drift (a
timestamp, a request id, the cash figure in a reconciliation mismatch).
This is where "restart forever with nobody knowing" would be unacceptable,
so two things happen:

1. **Automatic**: `scripts/run_agent.py`'s `main()` persists the failing
   exception's type and message, and how many times in a row that type has
   recurred, next to the audit log (`agent/failure_sentinel.py`; no extra
   file path to configure). Recurrence is keyed on `type(exc).__name__`,
   not the message text -- a `ReconciliationError` reporting a different
   dollar amount each time is still recognized as the same permanent
   failure. Once the same exception type has recurred 3 times in a row, it
   fires a real macOS desktop notification (`osascript`) titled
   "investmentagent", naming the recurrence count, the exception type, and
   the latest message. A single occurrence never notifies -- only a
   failure type that keeps recurring does. A failed notification attempt
   (e.g. no GUI session) is caught and logged as a warning; it never
   changes the process's exit code or masks the underlying failure, which
   is already in the error log either way.
2. **Manual fallback**, for whenever the automatic notification is missed,
   suppressed, or the machine was asleep:
   - `launchctl list com.investmentagent.reconcile-loop` shows the job's
     current PID (if running) and its last exit code.
   - Tail `StandardErrorPath` (the `.err.log` file from step 2) to see the
     actual repeating traceback/message.
   - The sentinel file itself (`state/failure_sentinel.json`) is plain
     JSON: `exc_type`, `message`, `first_at`, `last_at`, `consecutive_count`
     -- readable directly if you want the recurrence history without
     digging through logs.

Neither path fixes the underlying problem (an expired credential still
needs to be renewed, a locked keychain still needs to be unlocked) --
they exist so an operator finds out promptly rather than discovering days
later that the loop has been silently failing the whole time.

# Installing the operator dashboard (`com.investmentagent.dashboard`)

This installs `scripts/run_dashboard.py` (§10; operator decision surface
unit) as a second, separate macOS LaunchAgent, so the dashboard is
reachable at `http://127.0.0.1:8765/` without a terminal open. Until this
job existed there was no way to run the dashboard unattended at all --
launched by hand, it would stop the moment the terminal it was started in
closed.

**Reads the SAME durable state as the reconciliation loop, not a second
copy of it.** Point this job's `--data-dir` at the exact same directory
as the reconciliation loop's own `--data-dir` (step 2 above) -- the
dashboard attaches a read/decide surface onto the cost ledger, approval
requests, opportunity tracker, and audit log a real `com.investmentagent.
reconcile-loop` job is writing to, using the identical filenames (see
`scripts/run_dashboard.py`'s own module docstring). It never constructs a
broker adapter and never touches a credential (see `agent/dashboard_
server.py`'s own "what this surface must never do").

## 1. Fill in the placeholders

Copy `deploy/com.investmentagent.dashboard.plist` to
`~/Library/LaunchAgents/com.investmentagent.dashboard.plist` and replace
every `/REPLACE/...` value:

- the absolute path to `scripts/run_dashboard.py` in your checkout of this repo
- the absolute path to your `config.json`
- `--account-id` (optional for this script, but recommended -- without it,
  `GET /api/state`'s `approvals.outstanding_earmarks_usd` reports `null`
  with an unavailable-reason string instead of a real figure)
- `--data-dir`: the SAME absolute path as the reconciliation loop's own
  `--data-dir`
- `StandardOutPath`/`StandardErrorPath`: paths under the `logs/` directory
  created in step 1 of the reconciliation loop's own install (a second
  pair of log files, not shared with the loop's own)

`--host`/`--port` are already filled in (`127.0.0.1`/`8765`) and should
stay that way -- `agent.dashboard_server.make_server` refuses any
non-loopback host outright regardless of what this file says.

## 2. Load the job

```sh
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.investmentagent.dashboard.plist
```

Then open `http://127.0.0.1:8765/` in a browser on the same machine.

**Known gap, disclosed, not fixed here:** `scripts/run_dashboard.py` does
not construct a broker adapter or a `DayTradeGuard`, so `GET /api/state`'s
`risk_gates.current_reserve_pct`/`reconciliation.day_trade_count` report
`null` with an unavailable-reason string even once this job is running --
see this unit's own report for what wiring those in for real would
require.

## Uninstalling

```sh
launchctl bootout gui/$(id -u)/com.investmentagent.reconcile-loop
launchctl bootout gui/$(id -u)/com.investmentagent.dashboard
```
