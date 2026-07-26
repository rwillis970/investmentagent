"""Mode state machine transitions (§9.2).

Membership validation (`mode in MODES`) proves a string is a known mode. It
proves nothing about whether *reaching* that mode from where the system
actually was is legal -- which is why "DISABLED to PRODUCTION_ACTIVE in one
step is impossible" (§12 criterion 3, and the Day-1 exit criterion in §11)
was previously enforced by nothing at all. This module is the enforcement.

    DISABLED <-> RESEARCH <-> PAPER <-> PRODUCTION_ACTIVE <-> PAUSED

Forward movement is one step along the chain at a time. DISABLED and PAUSED
are the exception: reachable immediately and unconditionally from any state,
because a kill switch must never be blocked by the same state machine it
exists to override -- PAUSED is three steps from DISABLED on the chain, and
that transition still has to be immediate.

The PAPER -> PRODUCTION_ACTIVE edge additionally requires explicit
confirmation. Real re-authentication against live broker credentials is a
Day-10 concern (a separate keychain entry, a separate process) and is out of
scope here; this module only guarantees the edge cannot be crossed silently
by a config value alone -- `confirmed` is the config-level half of that gate.
"""
from __future__ import annotations

CHAIN = ("DISABLED", "RESEARCH", "PAPER", "PRODUCTION_ACTIVE", "PAUSED")

# Reachable in one step from ANY state, unconditionally -- not merely
# "adjacent". This is a deliberate exception to the one-step rule, not a
# consequence of it.
IMMEDIATE_TARGETS = frozenset({"DISABLED", "PAUSED"})

# Edges that are legal one-step moves but additionally require explicit
# confirmation before they may be taken.
CONFIRMATION_REQUIRED = frozenset({("PAPER", "PRODUCTION_ACTIVE")})

_POSITION = {name: i for i, name in enumerate(CHAIN)}


class ModeTransitionError(Exception):
    """Base for every way a mode load can be refused. A readable startup
    error, per the Day-1 exit criterion -- never a warning, never a clamp."""


class IllegalModeTransition(ModeTransitionError):
    pass


class ConfirmationRequired(ModeTransitionError):
    pass


def _check_known(value: str, *, where: str) -> None:
    if value not in _POSITION:
        raise ModeTransitionError(
            f"{where}: unknown mode {value!r}; must be one of {CHAIN}"
        )


def is_legal_step(persisted: str, target: str) -> bool:
    """Whether `target` is directly reachable in one step from `persisted`,
    per §9.2. Ignores the confirmation requirement on the guarded edge --
    that is a separate concern, checked by `assert_legal_startup`, because
    "is this edge on the graph" and "has this edge been confirmed" are
    different questions with different failure modes."""
    _check_known(persisted, where="is_legal_step")
    _check_known(target, where="is_legal_step")
    if target == persisted:
        return True
    if target in IMMEDIATE_TARGETS:
        return True
    return abs(_POSITION[persisted] - _POSITION[target]) == 1


def assert_legal_startup(persisted_mode: str | None, target_mode: str, *,
                         confirmed: bool = False) -> None:
    """Raise unless loading `target_mode` is legal given the mode the system
    was last persisted in.

    `persisted_mode=None` means no prior recorded state -- a fresh install --
    and is treated as DISABLED, the Day-1 default, never as "anything goes".
    This is what makes "a DISABLED-state install loading mode:
    PRODUCTION_ACTIVE fails to start" true even before any mode has ever
    actually been persisted.
    """
    persisted = persisted_mode if persisted_mode is not None else "DISABLED"
    _check_known(persisted, where="assert_legal_startup persisted_mode")
    _check_known(target_mode, where="assert_legal_startup target_mode")

    if not is_legal_step(persisted, target_mode):
        raise IllegalModeTransition(
            f"cannot load mode {target_mode!r}: not reachable in one step "
            f"from the persisted mode {persisted!r} along {CHAIN} (§9.2). "
            "Refusing to start rather than silently adopting a mode more "
            "than one step away."
        )
    if (persisted, target_mode) in CONFIRMATION_REQUIRED and not confirmed:
        raise ConfirmationRequired(
            f"{persisted} -> {target_mode} requires explicit confirmation "
            "before it will be accepted at load (§9.2). Re-authentication "
            "against live broker credentials happens separately, at the "
            "adapter layer (Day 10); this is the config-level half of that "
            "gate and does not stand in for it."
        )
