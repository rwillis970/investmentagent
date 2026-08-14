# Unit E — dashboard PAPER-vs-PAUSED truth (reconstructed 2026-08-13)

STATUS OF PRIOR REPORT: a previous session reported this unit as designed,
implemented and tested, on the same now-lost `/tmp` worktree described in
Units A/B's docs. That report is UNVERIFIED. Independently checked against
the CURRENT real-repo source: `grep -n "operational_state\|mode_store\|
ModeStore" agent/dashboard_state.py agent/dashboard_server.py
scripts/run_dashboard.py` returned zero matches before this unit — the
distinction did not exist anywhere in the dashboard code. Built fresh.

## The bug

`agent/dashboard_state.py::build_dashboard_state` exposed exactly one
mode-related field: `"mode": config.mode` — the BROKER ENVIRONMENT (which
Alpaca endpoint/credential namespace this process talks to: PAPER vs live),
set once at process construction from `--mode`/config, never updated at
runtime. Nothing anywhere read `agent.mode_store.ModeStore` — the durable,
append-only, cross-process PERSISTED OPERATIONAL STATE (PRODUCTION_ACTIVE/
PAUSED/DISABLED) a running `scripts/run_agent.py` process (and an operator's
`--advance-mode-to`) actually writes to. A dashboard showing `mode: "PAPER"`
had no way to distinguish "PAPER and actively trading" from "PAPER and
PAUSED" from "PAPER and DISABLED" — exactly the standing live-Mac
checkpoint's own described state (broker environment PAPER, persisted
operational state PAUSED pending reconciliation).

## The fix

- `agent/dashboard_state.py::build_dashboard_state` gained `operational_state:
  str | None = None` / `operational_state_paused_from: str | None = None`
  params. Return dict gained `"broker_environment": config.mode` (a new,
  clearer-named alias — `"mode"`'s value and meaning are UNCHANGED, to avoid
  breaking any existing caller/test/frontend expectation), plus both new
  fields rendered via the module's own pre-existing `_prefixed`/`_null`/
  `_present` honesty convention — `None` renders as an explicit "not
  supplied" with a reason string, never silently omitted or defaulted to a
  real-looking value.
- `agent/dashboard_server.py::DashboardRuntime` gained
  `operational_state_refresh_fn: Callable[[], tuple[str | None, str |
  None]] | None = None`, mirroring the pre-existing `broker_state_
  refresh_fn` field exactly (same per-request-refresh reasoning: the
  dashboard and the real `run_agent.py` scheduled loop are separate OS
  processes under separate LaunchAgents, so a value captured once at
  dashboard startup goes stale the moment the other process changes it).
  `route_request`'s `GET /api/state` handler now calls it per-request,
  same pattern as the broker-state refresh immediately above it.
- `scripts/run_dashboard.py::build_dashboard_runtime` gained a
  `mode_store_path: str | Path | None = None` param and a
  `_refresh_operational_state()` closure: constructs a FRESH `ModeStore`
  on every call (not held open — `ModeStore.__init__` loads its history
  once into memory and never re-reads its file, confirmed from that
  class's own docstring), translates `.current() is None` to the literal
  string `"DISABLED"` (that IS `ModeStore`'s own documented fresh-install
  baseline, a real value — not a stand-in for "unknown"), and degrades any
  exception to `(None, None)` — never raises, matching every other refresh
  path in this module. `--mode-store-path` is a new CLI flag, defaulting
  to `<data-dir>/mode_state.jsonl` — the SAME filename `scripts/
  run_agent.py`'s own `_DEFAULT_STORE_FILENAMES` uses, so pointing both
  scripts' `--data-dir` at the same directory resolves to the same file,
  not a second independently-named copy (matching this module's own
  established convention for every other shared store).

## Tests (all new, all passing)

- `tests/test_dashboard_state.py` — 5 new tests: `broker_environment` is a
  new alias for the unchanged `mode` value; `operational_state` defaults to
  an honest null when not supplied; **PAPER broker environment + PAUSED
  operational state are both visible simultaneously, disagreeing, never one
  masking the other** (the exact bug this unit closes); `"DISABLED"` is a
  real PRESENT value, not confused with "unavailable"; `paused_from` is
  null when the state is not PAUSED.
- `tests/test_dashboard_server.py` — 4 new tests, mirroring `broker_state_
  refresh_fn`'s own existing test shape exactly: refresh_fn is called
  per-request; a second request sees a value changed between polls; `None`
  (no refresh_fn wired) reports an honest unavailable, never fabricates;
  and the same PAPER+PAUSED co-existence proof at the `route_request` layer
  (not just `build_dashboard_state`'s own unit tests).
- `tests/test_run_dashboard.py` — 6 new tests, including the key
  cross-process-staleness proof: `test_operational_state_refresh_sees_a_
  change_written_by_a_separate_modestore_instance` constructs a
  `DashboardRuntime`, reads `"DISABLED"` from a fresh store, then writes a
  new mode via a SEPARATE `ModeStore` instance pointed at the same file
  (standing in for the real, separate `run_agent.py` process), and proves
  the very next `operational_state_refresh_fn()` call sees it — without
  any dashboard restart. Plus: never-written store reads `"DISABLED"`; no
  `--mode-store-path` given returns `(None, None)`; `paused_from` reads
  correctly when state is `"PAUSED"`; a corrupt store file degrades to
  `(None, None)`, never raises. Also fixed 1 pre-existing test
  (`test_data_dir_is_never_created_when_every_store_path_is_explicit`) to
  supply `--mode-store-path` explicitly, since it is now one of the store
  paths that flag is checking.

Full suite after Unit E: 4849 Python tests passed (4835 baseline after
Unit B + 14 new), 34/34 JS tests passed.

## What this unit did NOT do (disclosed, matching the same decision as the
lost prior report's own disclosed gap)

`dashboard/static/agent_command_center.html` is a single-line, ~1.06MB
minified file (confirmed via `wc -c`) — no visible PAPER-vs-PAUSED UI label
was added to it. Hand-editing a minified file this size, verifying the edit
did not corrupt it, under the standing "do not touch the live runtime
tonight" constraint, was judged too risky relative to the value of a purely
cosmetic frontend change when the backend/API contract (the actual
information the frontend would need to render such a label) is now
complete and thoroughly tested. The `--data-dir`-only deployment plist
(`deploy/com.investmentagent.dashboard.plist`) already passes only
`--data-dir`, not individual store paths — confirmed via direct read — so
this fix activates automatically on the next ordinary dashboard restart, no
plist change needed.
