"""Secrets provider (§8, Day 2 gap).

§8's local-deployment table specifies OS keychain, separate entries per mode,
behind a provider interface whose migration seam is a managed vault.
`agent.accounts.BrokerCredentials` already models a credential as a
REFERENCE -- `key_id` plus `secret_ref`, never a raw secret (see
accounts.py). This module is the provider that resolves those references
into an actual secret value, at the point of use.

MODE ISOLATION IS STRUCTURAL, NOT A PERMISSION CHECK (§1.2, §8: "Paper and
live modes, credential-isolated"). A `SecretsProvider` is bound to exactly
ONE mode at construction, and `resolve(secret_ref)` takes no mode argument --
there is no call shape that could ask a PAPER-bound provider for a live
entry. For `KeychainSecretsProvider` this is enforced by namespacing the
keychain "service" name with the mode (`_service_name`), so a PAPER-bound
provider's `security` invocation never even names the service a live entry
would be stored under. For `InMemorySecretsProvider` it is enforced by each
instance owning its own private `dict`. Neither implementation has an escape
hatch; a caller that wants a different mode's secret must construct a
different provider, which means going through whatever gates that
construction (out of scope for this module -- there is no runtime
mode-transition path yet; see agent/mode_store.py and agent/startup.py).

NO SECRET VALUE IS EVER LOGGED, INCLUDED IN AN EXCEPTION MESSAGE, OR WRITTEN
ANYWHERE THIS MODULE DOES NOT HAND IT DIRECTLY BACK TO THE IMMEDIATE CALLER.
`SecretNotFoundError` carries only the reference used to look a secret up
(mode, secret_ref) -- never a value, and never a chained cause from the
underlying subprocess failure, since a `CalledProcessError`'s own `.cmd`/
`.stderr` could otherwise surface unexpected keychain output in a traceback.
This module does not itself write to `agent.audit.AuditLog`; nothing here
constructs a payload that could reach it.

A MISSING CREDENTIAL IS A HARD ERROR. Neither implementation ever returns
"" or None for an absent entry -- both raise `SecretNotFoundError`, matching
the codebase's general fail-safe-to-NO-TRADE discipline (a credential that
silently resolved to an empty string would be a broker call with a blank
API key, not a refusal to trade).

PROVISIONING (writing a new secret into the OS keychain) IS OUT OF SCOPE
for this module. This is the resolve-only half of the interface the prompt
asked for; an operator provisions entries out-of-band with the `security`
CLI directly (`security add-generic-password -s <service> -a <secret_ref>
-w <value>`), never by round-tripping a raw secret value through this
codebase's own argv or logs.

KEYCHAIN MECHANISM AND HEADLESS/SLEEP BEHAVIOUR (asked for explicitly):
`KeychainSecretsProvider` shells out to `/usr/bin/security`, the CLI bundled
with every macOS install -- not a pip package, so this adds no dependency
(pyproject.toml stays empty, per this unit's constraint; the `keyring`
PyPI package was the alternative and was deliberately not used). It reads
from the user's LOGIN keychain, which unlocks at login and, under macOS's
default settings, stays unlocked across sleep -- so this works unattended
on a laptop that sleeps, with no re-prompt needed on wake. The one caveat:
if the user has enabled "Lock keychain when sleeping" in Keychain Access,
`security` fails after wake until the login keychain is unlocked again by
someone present at the machine, and `resolve` raises `SecretNotFoundError`
exactly as it would for a genuinely absent entry. That is a fail-safe
outcome under this codebase's own NO-TRADE-on-uncertainty invariant, not a
bug to special-case or route around.
"""
from __future__ import annotations

import subprocess
import time
from abc import ABC, abstractmethod
from typing import Callable

_SERVICE_PREFIX = "investmentagent"


def _service_name(mode: str) -> str:
    return f"{_SERVICE_PREFIX}:{mode}"


class SecretNotFoundError(Exception):
    """A missing (or unreadable) credential is a hard error, never a
    silently-returned empty string. Carries only the reference used to look
    it up -- mode and secret_ref -- never a secret value, and never a
    chained cause: raised with `from None` everywhere, specifically so that
    an underlying subprocess failure's own message/stderr (which could
    contain unexpected keychain output) never rides along in a traceback."""

    def __init__(self, *, mode: str, secret_ref: str):
        self.mode = mode
        self.secret_ref = secret_ref
        super().__init__(
            f"no secret found for mode={mode!r} secret_ref={secret_ref!r}"
        )


class SecretsProvider(ABC):
    """Bound to exactly one mode at construction. `resolve` takes no mode
    argument -- see module docstring for why that is the actual isolation
    mechanism, not a permission check layered on top of a shared
    namespace."""

    def __init__(self, mode: str):
        self._mode = mode

    @property
    def mode(self) -> str:
        return self._mode

    @abstractmethod
    def resolve(self, secret_ref: str) -> str:
        """Return the secret value for `secret_ref`, scoped to this
        provider's mode. Raises SecretNotFoundError if absent -- never
        returns "" or None."""


class KeychainSecretsProvider(SecretsProvider):
    """Real implementation: the macOS login keychain, via the `security`
    CLI (bundled with the OS -- see module docstring for why this, not the
    `keyring` package). Each entry's keychain "service" is namespaced by
    mode via `_service_name`; `secret_ref` is the keychain "account" within
    that service. A provider bound to mode=PAPER only ever invokes `security
    find-generic-password -s investmentagent:PAPER ...` -- it has no code
    path that could form a query against `investmentagent:PRODUCTION_ACTIVE`.
    """

    def resolve(self, secret_ref: str) -> str:
        try:
            result = subprocess.run(
                ["security", "find-generic-password",
                 "-s", _service_name(self.mode), "-a", secret_ref, "-w"],
                capture_output=True, text=True, check=True,
            )
        except (subprocess.CalledProcessError, FileNotFoundError):
            # Deliberately not `from exc`: a CalledProcessError's own
            # __str__/`.cmd`/`.stderr` could carry unexpected keychain
            # output, and that must never ride along in what this raises.
            raise SecretNotFoundError(mode=self.mode, secret_ref=secret_ref) from None
        value = result.stdout.rstrip("\n")
        if not value:
            # Exit 0 with empty stdout is indistinguishable from a stored
            # empty string, and an empty string is not a usable credential
            # either way -- both collapse to the same hard error rather than
            # a silent empty-string secret being handed to a broker call.
            raise SecretNotFoundError(mode=self.mode, secret_ref=secret_ref)
        return value


class InMemorySecretsProvider(SecretsProvider):
    """Test double. Never touches the real keychain. Entries live in a
    private dict on this instance only -- two instances at different modes
    are independent regardless of overlapping secret_ref values, the same
    isolation property `KeychainSecretsProvider` gets from namespacing."""

    def __init__(self, mode: str, entries: dict[str, str] | None = None):
        super().__init__(mode)
        self._entries: dict[str, str] = dict(entries or {})

    def put(self, secret_ref: str, value: str) -> None:
        self._entries[secret_ref] = value

    def resolve(self, secret_ref: str) -> str:
        try:
            return self._entries[secret_ref]
        except KeyError:
            raise SecretNotFoundError(mode=self.mode, secret_ref=secret_ref) from None


class CachingSecretsProvider(SecretsProvider):
    """UNIT A FIX (reconstructed 2026-08-13, Keychain prompt storm).

    THE DEFECT THIS CLOSES. `agent.broker.alpaca.AlpacaPaperAdapter.
    _headers()` calls `self._secrets.resolve(...)` fresh on every real
    HTTP call, by design (see that module's own CREDENTIALS section) --
    and `agent.broker.selection.select_broker_adapter` calls `.resolve()`
    once more, as a fail-fast presence check, before constructing anything.
    Neither `KeychainSecretsProvider` nor `InMemorySecretsProvider` caches
    anything (this module's own long-standing docstring: "RESOLVED FRESH,
    NEVER CACHED HERE") -- that design note was written to describe a
    generic `SecretsProvider` contract, but for the concrete
    `KeychainSecretsProvider` implementation, "resolve fresh" means a real
    `/usr/bin/security find-generic-password` subprocess invocation, which
    macOS can gate behind a per-invocation Keychain-access confirmation
    prompt depending on that item's ACL. Measured (tests/test_run_
    dashboard.py::test_measured_resolve_count_per_steady_state_refresh_is_
    four_uncached): FOUR resolve() calls per single, steady-state GET
    /api/state -- one presence check plus one per account()/positions()/
    open_orders() HTTP call -- and dashboard/static/dashboard_bind.js
    polls every 5 seconds (`POLL_INTERVAL_MS = 5000`), for the life of an
    open browser tab.

    THE FIX: a bounded-TTL cache wrapping any `SecretsProvider`, keyed by
    `secret_ref` (this provider is already bound to one mode, per the base
    class's own isolation contract -- see module docstring -- so no mode
    key is needed). A successful resolve is cached for `ttl_seconds`
    (default 300s = 5 minutes); a `SecretNotFoundError` is NEVER cached --
    a transient failure (e.g. the login keychain briefly locked after
    sleep, per this module's own KEYCHAIN MECHANISM section) must be
    retried on the very next call, not remembered as permanently absent,
    matching this codebase's fail-safe-to-NO-TRADE discipline: caching an
    absence could paper over a credential becoming available again without
    a restart, but never the reverse (a cached PRESENT value going briefly
    stale for up to `ttl_seconds` is the only risk accepted here, and 5
    minutes is short relative to any credential-rotation operator
    workflow, which is out-of-band and manual regardless -- see this
    module's own PROVISIONING section).

    NOT a general-purpose cache: no eviction beyond TTL expiry, no size
    bound (this codebase resolves at most a handful of distinct
    secret_refs per process: alpaca_api_secret, and optionally a
    gatekeeper signing key), and `now_fn` is injectable purely for tests
    (production leaves it `None` and gets `time.monotonic`, immune to
    real-wall-clock changes across a sleep/wake cycle)."""

    def __init__(self, wrapped: SecretsProvider, *, ttl_seconds: float = 300.0,
                now_fn: Callable[[], float] | None = None):
        super().__init__(wrapped.mode)
        self._wrapped = wrapped
        self._ttl_seconds = ttl_seconds
        self._now_fn = now_fn or time.monotonic
        self._cache: dict[str, tuple[str, float]] = {}   # secret_ref -> (value, cached_at)

    def resolve(self, secret_ref: str) -> str:
        cached = self._cache.get(secret_ref)
        if cached is not None:
            value, cached_at = cached
            if self._now_fn() - cached_at < self._ttl_seconds:
                return value
        value = self._wrapped.resolve(secret_ref)   # SecretNotFoundError propagates, never cached
        self._cache[secret_ref] = (value, self._now_fn())
        return value


def default_keychain_secrets_provider_factory(mode: str) -> SecretsProvider:
    """The ONE production `secrets_provider_factory` default -- used by
    BOTH `scripts/run_agent.py` and `scripts/run_dashboard.py` (previously
    each used bare `KeychainSecretsProvider` directly as their own default,
    two independent call sites making the same "wrap it in a cache" change
    would otherwise have needed applying twice). Returns a
    `CachingSecretsProvider` wrapping a real `KeychainSecretsProvider` for
    `mode` -- see `CachingSecretsProvider`'s own docstring for why (Unit A,
    the Keychain prompt storm fix). Tests never use this: they inject
    `InMemorySecretsProvider`-based factories directly, exactly as before
    this function existed."""
    return CachingSecretsProvider(KeychainSecretsProvider(mode))
