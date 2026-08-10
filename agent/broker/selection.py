"""Single broker-adapter selection point (config-driven-broker-selection
unit, 2026-08-10).

THE DEFECT THIS CLOSES. Before this module: `scripts/run_dashboard.py`'s
`_build_broker_state` constructed a bare `agent.broker.simulator.
SimulatorBroker` directly, and `scripts/run_agent.py` constructed a bare
`agent.broker.alpaca.AlpacaPaperAdapter` directly at two separate call
sites (`_real_adapter_factory`, the `--submit-approved` handler) -- three
independent places each making its own "which adapter" decision, none of
them reading any config value to make it. `select_broker_adapter`, below,
is now the one place that decision lives: it reads `cfg.broker`
("simulator" or "alpaca_paper" -- `agent.config.BROKER_TYPES`) and
constructs the matching adapter, forwarding `capability_policy`/
`staging_key` identically regardless of which type it builds.

`scripts/run_dashboard.py`'s `_build_broker_state` is wired through this
function (see that module). `scripts/run_agent.py` is NOT wired through it
in this same commit -- see this unit's own report for why: its real
construction call sites are exercised by tests/test_run_agent.py's
`--submit-approved` suite via `monkeypatch.setattr(run_agent_module,
"AlpacaPaperAdapter", ...)`, and every one of those tests' configs comes
from `config.example.json` (via `base_config()`), which names no `broker`
key -- so routing those call sites through this function, with `cfg.broker`
defaulting to "simulator", would silently stop constructing
`AlpacaPaperAdapter` at all in those tests (breaking them) unless
`config.example.json` itself were given an explicit `"broker":
"alpaca_paper"` -- which is indistinguishable from enabling alpaca_paper in
the one file most likely to be copied into a real deployment, directly
against this unit's own "do NOT switch the default to alpaca_paper, do not
enable it anywhere" instruction. This is a genuine, verified conflict
between two of this unit's own requirements, not an assumption -- reported,
not resolved unilaterally; see the final report.

DEFAULT IS SIMULATOR (`agent.config.Config.broker`'s own default -- see
that field's comment). AN UNRECOGNISED VALUE RAISES HERE TOO, belt and
suspenders alongside `agent.config.validate`'s own membership check: a
`Config` can be constructed directly (bypassing `load`/`validate`,
including in tests), and this function must never fall back to the
simulator silently for a value it doesn't recognise -- a typo would
otherwise quietly trade nothing while looking healthy.

CREDENTIALS COME FROM `agent.secrets_provider` ONLY, RESOLVED FRESH, NEVER
CACHED HERE. For the alpaca_paper branch, this function calls
`secrets_provider.resolve(credentials.secret_ref)` exactly ONCE, before
constructing anything, and discards the returned value immediately -- a
pure fail-fast presence check. This is necessary, not decorative:
`AlpacaPaperAdapter.__init__` itself never resolves the secret (it resolves
fresh on every `_headers()` call instead -- see agent/broker/alpaca.py's
own CREDENTIALS section), so without this probe a missing credential would
not surface until the first real HTTP call, long after "construct an
adapter" had already appeared to succeed. This unit's own requirement is
that a missing credential raises AND does not construct an adapter at all
-- both are true here: `SecretNotFoundError` is caught and re-raised as
`BrokerSelectionError`, naming the missing secret_ref and pointing at
docs/credentials-setup.md (not touched by this unit), before
`alpaca_adapter_cls(...)` is ever called.

STAGING KEY IS NOT REQUIRED TO CONSTRUCT EITHER ADAPTER TYPE (verified, not
assumed -- see tests/test_broker_selection.py's own staging-key tests,
mirroring tests/test_broker_alpaca.py's `test_submit_without_staging_key_
refuses`). `staging_key` is forwarded, whatever it is (including `None`),
to whichever adapter's constructor -- both accept it identically, and
neither refuses anything at construction time. The refusal this unit asked
to be verified lives in `BrokerAdapter._verify_staged_or_raise`
(agent/broker/base.py), inherited UNMODIFIED by every subclass:
`submit()`/`cancel()` raise `StagingKeyUnset` if `self._staging_key is
None`, regardless of adapter type or how it was constructed. This function
does not weaken that fail-safe -- it has no code path that could.

`simulator_cls`/`alpaca_adapter_cls` are injectable (defaulting to the real
classes) purely so a caller can prove "no adapter was constructed" with a
factory that raises if called -- see tests/test_broker_selection.py's own
use of this. This is not a general plugin mechanism; production code never
overrides either default.
"""
from __future__ import annotations

from datetime import datetime
from typing import Callable

from ..accounts import BrokerCredentials
from ..policy import TradeCapabilityPolicy
from ..secrets_provider import SecretNotFoundError, SecretsProvider
from .alpaca import AlpacaPaperAdapter
from .base import BrokerAdapter
from .simulator import SimulatorBroker
from .transport import Transport

# Imported lazily-by-name at call time (not at module import time) to avoid
# a circular import: agent.config does not import this module, so importing
# it here at module scope is safe, but kept as an explicit `from .. import
# config` (not `from ..config import Config`) so the BROKER_TYPES membership
# message below always reflects agent.config's own current tuple, not a
# frozen copy.
from .. import config as config_module


class BrokerSelectionError(Exception):
    """Raised by `select_broker_adapter`: an unrecognised `cfg.broker`
    value, or an `alpaca_paper` selection missing its required credentials
    or secrets_provider, or unable to resolve its credential. Never raised
    after an adapter has been constructed -- every raise below happens
    strictly before the matching `*_cls(...)` call."""


def select_broker_adapter(
    cfg: config_module.Config,
    *,
    account_id: str,
    credentials: BrokerCredentials | None = None,
    secrets_provider: SecretsProvider | None = None,
    capability_policy: TradeCapabilityPolicy | None = None,
    staging_key: bytes | None = None,
    now: datetime | None = None,
    transport: Transport | None = None,
    simulator_cls: Callable[..., BrokerAdapter] = SimulatorBroker,
    alpaca_adapter_cls: Callable[..., BrokerAdapter] = AlpacaPaperAdapter,
) -> BrokerAdapter:
    """The one place `cfg.broker` is turned into a real `BrokerAdapter`.
    See this module's own docstring for the full contract. `now` is only
    ever used by the simulator branch (SimulatorBroker's own clock);
    `transport` is only ever used by the alpaca_paper branch."""
    if cfg.broker == "simulator":
        return simulator_cls(
            account_id=account_id, credentials=credentials, now=now,
            capability_policy=capability_policy, staging_key=staging_key,
        )

    if cfg.broker == "alpaca_paper":
        if credentials is None:
            raise BrokerSelectionError(
                "broker=alpaca_paper requires credentials (key_id/secret_ref) -- "
                "none were given to select_broker_adapter. Refusing to construct "
                "an adapter."
            )
        if secrets_provider is None:
            raise BrokerSelectionError(
                "broker=alpaca_paper requires a secrets_provider to resolve "
                f"{credentials.secret_ref!r} -- none was given to "
                "select_broker_adapter. Refusing to construct an adapter."
            )
        try:
            # Fail-fast presence check only -- the value itself is discarded
            # immediately. AlpacaPaperAdapter resolves its own secret fresh
            # on every real HTTP call (see agent/broker/alpaca.py); this
            # probe exists purely so a missing credential is caught HERE,
            # before any adapter is constructed, per this unit's own
            # requirement.
            secrets_provider.resolve(credentials.secret_ref)
        except SecretNotFoundError:
            raise BrokerSelectionError(
                f"broker=alpaca_paper: no credential found for keychain entry "
                f"{credentials.secret_ref!r} (mode={secrets_provider.mode!r}). "
                "Provision it before selecting alpaca_paper -- see "
                "docs/credentials-setup.md. Refusing to construct an adapter."
            ) from None

        kwargs = dict(
            account_id=account_id, credentials=credentials,
            secrets_provider=secrets_provider, capability_policy=capability_policy,
            staging_key=staging_key,
        )
        if transport is not None:
            kwargs["transport"] = transport
        return alpaca_adapter_cls(**kwargs)

    raise BrokerSelectionError(
        f"cfg.broker={cfg.broker!r} is not a recognised broker type -- must be "
        f"one of {config_module.BROKER_TYPES}. Refusing to fall back to the "
        "simulator silently: an unrecognised value must fail loudly, never "
        "quietly trade nothing while looking healthy."
    )
