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
- `--ledger-store-path`, `--quarantine-store-path`, `--mode-store-path`,
  `--audit-log-path`: paths under the `state/` directory created in step 1
- `StandardOutPath`/`StandardErrorPath`: paths under the `logs/` directory
  created in step 1

Do not put a raw secret anywhere in the plist itself.

## 3. Advance the persisted mode to PAPER (fresh install only)

A brand-new `state/mode_state.jsonl` starts DISABLED, and §9.2's one-step
rule means PAPER cannot be reached in one step -- and setting
`mode: RESEARCH` (or `PAPER`) in `config.json` and just loading the job
does NOT work either: the loop always constructs a broker adapter bound to
PAPER, which refuses outright if `cfg.mode` isn't PAPER too. Run these two
commands once, from the repo root, before loading the job for the first
time (`--confirmed` is not needed for either step):

```sh
python3 scripts/run_agent.py --mode-store-path ~/investmentagent/state/mode_state.jsonl \
  --audit-log-path ~/investmentagent/state/audit.jsonl --advance-mode-to RESEARCH
python3 scripts/run_agent.py --mode-store-path ~/investmentagent/state/mode_state.jsonl \
  --audit-log-path ~/investmentagent/state/audit.jsonl --advance-mode-to PAPER
```

Each prints and exits 0 on success (or exits 1 with a clear reason if the
step is illegal or out of order -- nothing is written to either store on a
refusal). Confirm with `--advance-mode-to PAPER` again: a target equal to
the already-persisted mode is a harmless no-op. Make sure `config.json`
itself also has `"mode": "PAPER"` before step 4 -- this only advances the
PERSISTED mode; the loop's own `target_mode` still comes from `config.json`
on every run.

## 4. Load the job

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

## Uninstalling

```sh
launchctl bootout gui/$(id -u)/com.investmentagent.reconcile-loop
```
