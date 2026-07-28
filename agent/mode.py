"""Mode state machine transitions (§9.2).

Membership validation (`mode in MODES`) proves a string is a known mode. It
proves nothing about whether *reaching* that mode from where the system
actually was is legal -- which is why "DISABLED to PRODUCTION_ACTIVE in one
step is impossible" (§12 criterion 3, and the Day-1 exit criterion in §11)
was previously enforced by nothing at all. This module is the enforcement.

    DISABLED <-> RESEARCH <-> PAPER <-> PRODUCTION_ACTIVE

Forward movement along THIS chain is one step at a time. DISABLED is the
exception: reachable immediately and unconditionally from any state,
because a kill switch must never be blocked by the same state machine it
exists to override. PAPER -> PRODUCTION_ACTIVE additionally requires
explicit confirmation -- real re-authentication against live broker
credentials is a Day-10 concern (a separate keychain entry, a separate
process) and is out of scope here; this module only guarantees the edge
cannot be crossed silently by a config value alone -- `confirmed` is the
config-level half of that gate.

PAUSED IS DELIBERATELY NOT A MEMBER OF `CHAIN` (real gap found running the
loop for the first time: PAUSED was a dead end). `agent.startup._halt`
forces PAUSED on any failed startup, and §9.2 makes PAUSED reachable
unconditionally from ANY state, because it is an emergency stop, not a rung
on the escalation ladder. The PREVIOUS model put PAUSED at the END of the
same linear `CHAIN` tuple used for ordinary forward/backward adjacency
(`abs(index difference) == 1`), which is the wrong shape for a state
reachable from everywhere, for two independent reasons:

  1. THE DEAD END. PAUSED's only "one-index-away" neighbour was
     PRODUCTION_ACTIVE (the previous element in the tuple). A system paused
     from DISABLED, RESEARCH, or PAPER had no legal one-step path back to
     where it actually was -- only DISABLED (discarding all memory of the
     prior mode and forcing a full re-climb through RESEARCH/PAPER) or
     PRODUCTION_ACTIVE (requiring confirmation AND, separately, a live
     adapter that does not exist -- see scripts/run_agent.py's own
     docstring). A single failed first startup left no way back to
     operating at all short of hand-editing the mode store.

  2. THE ESCALATION BYPASS (found while designing the fix for #1 -- more
     serious, and NOT what was originally reported). Because PAUSED ->
     PRODUCTION_ACTIVE was "legal" (one index away) regardless of what mode
     PAUSED was actually entered from, and entering PAUSED is unconditional
     from ANYWHERE, the OLD model permitted DISABLED -> PAUSED (one hop,
     unconditional) -> PRODUCTION_ACTIVE (one hop, merely confirmed) as a
     two-hop path to live trading from an install that had NEVER actually
     operated in RESEARCH or PAPER -- silently defeating the entire point
     of the one-step escalation rule. This was not exploitable end-to-end
     only because no live adapter exists yet (a separate, accidental
     mitigation); the hole was real regardless.

THE FIX: PAUSED is removed from `CHAIN` (the escalation ordering) entirely
and modeled as its own case. Entering it remains unconditional from any
state, unchanged (`IMMEDIATE_TARGETS`). Leaving it is defined as returning
to the SPECIFIC mode it was paused from (`paused_from`, threaded through
`is_legal_step`/`assert_legal_startup` as a parameter -- these are pure
functions with no store access of their own; the caller, `agent.startup.
run_startup` and `scripts.run_agent._run_advance_mode`, resolves the real
value from `agent.mode_store.ModeStore.paused_from()`), or DISABLED (the
universal reset, unchanged). Resuming into PRODUCTION_ACTIVE still requires
confirmation, exactly as before -- but now ONLY when PRODUCTION_ACTIVE
really is the mode being resumed to (`paused_from == "PRODUCTION_ACTIVE"`),
closing bypass #2 above along with dead end #1. An unsupplied or unknown
`paused_from` defaults to allowing nothing but DISABLED -- default deny,
matching this codebase's other fail-safe-on-uncertainty gates (Appendix
E's "an unlisted value is DISABLED").
"""
from __future__ import annotations

# The real escalation ordering. PAUSED is deliberately NOT a member -- see
# module docstring's TOPOLOGY section. Forward/backward one-step adjacency
# among these four is still plain tuple-index arithmetic.
CHAIN = ("DISABLED", "RESEARCH", "PAPER", "PRODUCTION_ACTIVE")

# Every known mode value -- for MEMBERSHIP validation (agent.config.MODES
# aliases this), not transition legality. PAUSED is a real, valid mode
# value; it simply is not part of the escalation ordering above.
MODES = CHAIN + ("PAUSED",)

# Reachable in one step from ANY state, unconditionally -- not merely
# "adjacent". This is a deliberate exception to the one-step rule, not a
# consequence of it.
IMMEDIATE_TARGETS = frozenset({"DISABLED", "PAUSED"})

# Edges that are legal one-step moves but additionally require explicit
# confirmation before they may be taken. PAPER -> PRODUCTION_ACTIVE is the
# initial promotion; PAUSED -> PRODUCTION_ACTIVE is resuming into it,
# legal now ONLY when paused_from == "PRODUCTION_ACTIVE" (see
# is_legal_step) -- this set does not change, only what makes the PAUSED
# edge reachable at all in the first place does. Pausing itself (the
# reverse direction, into PAUSED) is deliberately NOT in this set -- PAUSED
# is a kill-switch target and must stay reachable unconditionally; only the
# way back out to live trading is gated.
CONFIRMATION_REQUIRED = frozenset({
    ("PAPER", "PRODUCTION_ACTIVE"),
    ("PAUSED", "PRODUCTION_ACTIVE"),
})

_POSITION = {name: i for i, name in enumerate(CHAIN)}
_KNOWN = frozenset(MODES)


class ModeTransitionError(Exception):
    """Base for every way a mode load can be refused. A readable startup
    error, per the Day-1 exit criterion -- never a warning, never a clamp."""


class IllegalModeTransition(ModeTransitionError):
    pass


class ConfirmationRequired(ModeTransitionError):
    pass


def _check_known(value: str, *, where: str) -> None:
    if value not in _KNOWN:
        raise ModeTransitionError(
            f"{where}: unknown mode {value!r}; must be one of {MODES}"
        )


def normalize_persisted(persisted_mode: str | None) -> str:
    """`None` (a fresh install, nothing ever persisted) means DISABLED --
    the Day-1 baseline `assert_legal_startup` already treats it as
    internally. Exposed here so every OTHER caller that needs the actual
    string, not just a legality answer -- specifically, recording what a
    PAUSED transition happened FROM -- uses the exact same convention,
    rather than each re-deriving "None means DISABLED" independently."""
    return persisted_mode if persisted_mode is not None else "DISABLED"


def is_legal_step(persisted: str, target: str, *, paused_from: str | None = None) -> bool:
    """Whether `target` is directly reachable in one step from `persisted`,
    per §9.2. Ignores the confirmation requirement on either guarded edge --
    that is a separate concern, checked by `assert_legal_startup`, because
    "is this edge on the graph" and "has this edge been confirmed" are
    different questions with different failure modes.

    `paused_from`, required only when `persisted == "PAUSED"`: the mode
    PAUSED was actually entered from (see `agent.mode_store.ModeStore.
    paused_from()`). It is NOT consulted for any other `persisted` value.
    See module docstring's TOPOLOGY section for why PAUSED's legal exits
    are {DISABLED, paused_from} specifically, never derived from CHAIN
    position -- an unsupplied or unknown paused_from allows nothing but
    DISABLED."""
    _check_known(persisted, where="is_legal_step")
    _check_known(target, where="is_legal_step")
    if paused_from is not None:
        _check_known(paused_from, where="is_legal_step paused_from")
    if target == persisted:
        return True
    if target in IMMEDIATE_TARGETS:
        return True
    if persisted == "PAUSED":
        # PAUSED is not a position in CHAIN -- see module docstring. Its
        # only legal one-step exits are DISABLED (handled above,
        # unconditionally) and the SPECIFIC mode it was paused from.
        return paused_from is not None and target == paused_from
    return abs(_POSITION[persisted] - _POSITION[target]) == 1


def assert_legal_startup(persisted_mode: str | None, target_mode: str, *,
                         confirmed: bool = False, paused_from: str | None = None) -> None:
    """Raise unless loading `target_mode` is legal given the mode the system
    was last persisted in.

    `persisted_mode=None` means no prior recorded state -- a fresh install --
    and is treated as DISABLED, the Day-1 default, never as "anything goes".
    This is what makes "a DISABLED-state install loading mode:
    PRODUCTION_ACTIVE fails to start" true even before any mode has ever
    actually been persisted.

    `paused_from`: see `is_legal_step`'s own docstring. Only meaningful (and
    only consulted) when `persisted_mode == "PAUSED"`; the caller is
    responsible for resolving it from `ModeStore.paused_from()` first.
    """
    persisted = normalize_persisted(persisted_mode)
    _check_known(persisted, where="assert_legal_startup persisted_mode")
    _check_known(target_mode, where="assert_legal_startup target_mode")

    if not is_legal_step(persisted, target_mode, paused_from=paused_from):
        detail = (f"along {CHAIN} (§9.2)" if persisted != "PAUSED"
                 else f"PAUSED's only legal exits are DISABLED and the mode "
                      f"it was paused from ({paused_from!r})")
        raise IllegalModeTransition(
            f"cannot load mode {target_mode!r}: not reachable in one step "
            f"from the persisted mode {persisted!r} -- {detail}. Refusing "
            "to start rather than silently adopting an illegal mode."
        )
    if (persisted, target_mode) in CONFIRMATION_REQUIRED and not confirmed:
        raise ConfirmationRequired(
            f"{persisted} -> {target_mode} requires explicit confirmation "
            "before it will be accepted at load (§9.2). Re-authentication "
            "against live broker credentials happens separately, at the "
            "adapter layer (Day 10); this is the config-level half of that "
            "gate and does not stand in for it."
        )
