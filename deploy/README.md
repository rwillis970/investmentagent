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

These must exist and be writable *before* the job is loaded -- launchd does
not create them, and `LedgerStore`/`ModeStore`/`AuditLog` all refuse to
silently create a parent directory that doesn't exist.

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
- `--ledger-store-path`, `--mode-store-path`, `--audit-log-path`: paths
  under the `state/` directory created in step 1
- `StandardOutPath`/`StandardErrorPath`: paths under the `logs/` directory
  created in step 1

Do not put a raw secret anywhere in the plist itself.

## 3. Load the job

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
genuine reconciliation halt) recurs identically on every relaunch. This is
where "restart forever with nobody knowing" would be unacceptable, so two
things happen:

1. **Automatic**: `scripts/run_agent.py`'s `main()` persists the failure
   message and how many times in a row it has recurred, next to the audit
   log (`agent/failure_sentinel.py`; no extra file path to configure).
   Once the *same* failure has recurred 3 times in a row, it fires a real
   macOS desktop notification (`osascript`) titled "investmentagent",
   naming the recurrence count and the error. A single occurrence never
   notifies -- only a failure that keeps recurring identically does. A
   failed notification attempt (e.g. no GUI session) is caught and logged
   as a warning; it never changes the process's exit code or masks the
   underlying failure, which is already in the error log either way.
2. **Manual fallback**, for whenever the automatic notification is missed,
   suppressed, or the machine was asleep:
   - `launchctl list com.investmentagent.reconcile-loop` shows the job's
     current PID (if running) and its last exit code.
   - Tail `StandardErrorPath` (the `.err.log` file from step 2) to see the
     actual repeating traceback/message.
   - The sentinel file itself (`state/failure_sentinel.json`) is plain
     JSON: `message`, `first_at`, `last_at`, `consecutive_count` -- readable
     directly if you want the recurrence history without digging through
     logs.

Neither path fixes the underlying problem (an expired credential still
needs to be renewed, a locked keychain still needs to be unlocked) --
they exist so an operator finds out promptly rather than discovering days
later that the loop has been silently failing the whole time.

## Uninstalling

```sh
launchctl bootout gui/$(id -u)/com.investmentagent.reconcile-loop
```
