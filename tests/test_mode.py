"""Mode transition guard (§9.2, §12 criterion 3, Day-1 exit criterion).

Before this, `mode` was validated only for tuple membership -- nothing
checked that the mode named in a *loaded* config was actually reachable
from where the system last was. That is why "DISABLED to PRODUCTION_ACTIVE
in one step is impossible" passed trivially: nothing asserted it at all.

    DISABLED <-> RESEARCH <-> PAPER <-> PRODUCTION_ACTIVE

Forward movement along THIS chain is one step at a time. DISABLED is
reachable immediately and unconditionally from any state -- the kill-switch
end of the chain. PAPER -> PRODUCTION_ACTIVE additionally requires explicit
confirmation (real re-authentication against live credentials is a Day-10
concern and out of scope here; this module only enforces that the edge
cannot be crossed silently by config alone).

PAUSED IS DELIBERATELY NOT A MEMBER OF THIS CHAIN (found running the loop
for the first time: PAUSED was a dead end). §9.2 makes PAUSED reachable
unconditionally from ANY state, because it is an emergency stop -- but
modeling it as occupying a fixed position in a linear ordering, and
deriving its legal exits from adjacency to that position, is the wrong
shape for a state reachable from everywhere: it made PAUSED's only exit
"the next index along the tuple" (PRODUCTION_ACTIVE, since PAUSED sat right
after it), which permanently stranded a system paused from DISABLED,
RESEARCH or PAPER (the only way out was PAUSED -> DISABLED, discarding all
memory of where it actually was and forcing a full re-climb) -- AND, worse,
accidentally made DISABLED -> PAUSED -> PRODUCTION_ACTIVE (two individually
"legal" hops, the second merely confirmed) a bypass of the entire one-step
escalation rule, reaching live trading without ever having actually run in
RESEARCH or PAPER.

The fix: PAUSED records the mode it was paused FROM (`paused_from`,
threaded through here as a parameter since `is_legal_step`/
`assert_legal_startup` are pure functions with no store access of their
own -- the caller, `agent.startup.run_startup`, resolves it from
`ModeStore.paused_from()`). PAUSED's only legal one-step exits become
DISABLED (unconditional, unchanged) and the SPECIFIC `paused_from` mode --
never "whatever is index-adjacent". Resuming into PRODUCTION_ACTIVE still
requires confirmation, exactly as before, but only when that really was the
mode being resumed to (`paused_from == "PRODUCTION_ACTIVE"`), not as a
blanket rule reachable from a pause entered anywhere. An unsupplied or
unknown `paused_from` defaults to allowing nothing but DISABLED -- default
deny, matching this codebase's other fail-safe-on-uncertainty gates.
"""
import pytest

from agent import mode as M


# -- is_legal_step: the chain, ignoring confirmation ------------------------

def test_adjacent_forward_steps_are_legal():
    assert M.is_legal_step("DISABLED", "RESEARCH")
    assert M.is_legal_step("RESEARCH", "PAPER")
    assert M.is_legal_step("PAPER", "PRODUCTION_ACTIVE")


def test_adjacent_backward_steps_are_legal():
    assert M.is_legal_step("RESEARCH", "DISABLED")
    assert M.is_legal_step("PAPER", "RESEARCH")
    assert M.is_legal_step("PRODUCTION_ACTIVE", "PAPER")


def test_same_mode_is_always_legal():
    for m in M.MODES:
        assert M.is_legal_step(m, m)


def test_multi_step_jump_is_illegal():
    assert not M.is_legal_step("DISABLED", "PAPER")
    assert not M.is_legal_step("DISABLED", "PRODUCTION_ACTIVE")
    assert not M.is_legal_step("RESEARCH", "PRODUCTION_ACTIVE")
    assert not M.is_legal_step("PRODUCTION_ACTIVE", "RESEARCH")


def test_disabled_and_paused_are_reachable_from_any_state_unconditionally():
    """The kill-switch property: DISABLED and PAUSED are always one legal
    step away from anywhere, regardless of chain position or paused_from."""
    for start in M.MODES:
        assert M.is_legal_step(start, "DISABLED")
        assert M.is_legal_step(start, "PAUSED")


def test_unknown_mode_is_rejected_not_silently_false():
    with pytest.raises(M.ModeTransitionError):
        M.is_legal_step("DISABLED", "SUSPENDED")
    with pytest.raises(M.ModeTransitionError):
        M.is_legal_step("SUSPENDED", "DISABLED")


# -- PAUSED is not part of the escalation chain ------------------------------

def test_paused_is_not_a_member_of_the_escalation_chain():
    assert "PAUSED" not in M.CHAIN
    assert "PAUSED" in M.MODES
    assert set(M.MODES) == set(M.CHAIN) | {"PAUSED"}


# -- leaving PAUSED: paused_from, not chain adjacency ------------------------

def test_resuming_from_paused_requires_the_specific_paused_from_mode():
    assert M.is_legal_step("PAUSED", "PAPER", paused_from="PAPER")
    assert not M.is_legal_step("PAUSED", "RESEARCH", paused_from="PAPER")
    assert not M.is_legal_step("PAUSED", "PRODUCTION_ACTIVE", paused_from="PAPER")


def test_resuming_from_paused_without_a_known_paused_from_only_allows_disabled():
    """Default deny: an unsupplied paused_from must not fall back to
    "anything reachable by the old chain-adjacency rule" -- only the
    universal DISABLED exit remains legal."""
    assert M.is_legal_step("PAUSED", "DISABLED")               # unaffected
    assert not M.is_legal_step("PAUSED", "PAPER")
    assert not M.is_legal_step("PAUSED", "RESEARCH")
    assert not M.is_legal_step("PAUSED", "PRODUCTION_ACTIVE")


def test_paused_from_itself_must_be_a_known_mode():
    with pytest.raises(M.ModeTransitionError):
        M.is_legal_step("PAUSED", "PAPER", paused_from="NOT_A_MODE")


# -- normalize_persisted ------------------------------------------------------

def test_normalize_persisted_treats_none_as_disabled():
    assert M.normalize_persisted(None) == "DISABLED"
    assert M.normalize_persisted("PAPER") == "PAPER"
    assert M.normalize_persisted("PRODUCTION_ACTIVE") == "PRODUCTION_ACTIVE"


# -- assert_legal_startup: the actual startup gate ---------------------------

def test_disabled_install_cannot_start_in_production_active():
    """The literal scenario the doc names: a DISABLED-state install loading
    mode: "PRODUCTION_ACTIVE" fails to start."""
    with pytest.raises(M.IllegalModeTransition):
        M.assert_legal_startup("DISABLED", "PRODUCTION_ACTIVE")


def test_no_persisted_state_is_treated_as_disabled_not_as_anything_goes():
    """A fresh install with no prior recorded mode is the Day-1 default of
    DISABLED, not a free pass to start anywhere."""
    with pytest.raises(M.IllegalModeTransition):
        M.assert_legal_startup(None, "PRODUCTION_ACTIVE")
    # DISABLED -> RESEARCH is one step and is fine even with no history.
    M.assert_legal_startup(None, "RESEARCH")


def test_disabled_install_can_start_in_research():
    M.assert_legal_startup("DISABLED", "RESEARCH")


def test_paper_to_production_active_requires_confirmation():
    with pytest.raises(M.ConfirmationRequired):
        M.assert_legal_startup("PAPER", "PRODUCTION_ACTIVE")
    with pytest.raises(M.ConfirmationRequired):
        M.assert_legal_startup("PAPER", "PRODUCTION_ACTIVE", confirmed=False)
    # Does not raise when confirmed.
    M.assert_legal_startup("PAPER", "PRODUCTION_ACTIVE", confirmed=True)


def test_paused_to_production_active_also_requires_confirmation():
    """Resuming into live trading after a pause is not exempt from the same
    re-authentication §9.2 requires of the initial PAPER promotion -- but
    only when PRODUCTION_ACTIVE really is the mode being resumed TO
    (paused_from must say so); see test_the_disabled_paused_production_
    active_bypass_is_closed for what happens when it isn't."""
    with pytest.raises(M.ConfirmationRequired):
        M.assert_legal_startup("PAUSED", "PRODUCTION_ACTIVE", paused_from="PRODUCTION_ACTIVE")
    with pytest.raises(M.ConfirmationRequired):
        M.assert_legal_startup("PAUSED", "PRODUCTION_ACTIVE", paused_from="PRODUCTION_ACTIVE",
                               confirmed=False)
    # Does not raise when confirmed.
    M.assert_legal_startup("PAUSED", "PRODUCTION_ACTIVE", paused_from="PRODUCTION_ACTIVE",
                           confirmed=True)


def test_confirmation_is_required_on_both_edges_into_production_active_and_nowhere_else():
    """Every other legal edge -- including the reverse of the guarded PAPER
    edge, and entering PAUSED itself -- needs no confirmation flag at all."""
    M.assert_legal_startup("PRODUCTION_ACTIVE", "PAPER")  # backward, no confirm needed
    M.assert_legal_startup("RESEARCH", "PAPER")
    M.assert_legal_startup("PRODUCTION_ACTIVE", "PAUSED")  # entering PAUSED: unguarded
    M.assert_legal_startup("DISABLED", "PAUSED")


def test_illegal_jump_is_reported_even_with_confirmed_true():
    """Confirmation authorizes crossing a *legal* guarded edge; it is not a
    general override for an illegal multi-step jump."""
    with pytest.raises(M.IllegalModeTransition):
        M.assert_legal_startup("DISABLED", "PRODUCTION_ACTIVE", confirmed=True)


def test_resuming_from_paused_returns_to_the_specific_mode_it_was_paused_from():
    """The actual fix, exercised through assert_legal_startup rather than
    is_legal_step directly: a system paused from RESEARCH resumes back to
    RESEARCH, one step, no confirmation -- not stranded, and not able to
    reach PAPER/PRODUCTION_ACTIVE just because they're "further along"."""
    M.assert_legal_startup("PAUSED", "RESEARCH", paused_from="RESEARCH")
    with pytest.raises(M.IllegalModeTransition):
        M.assert_legal_startup("PAUSED", "PAPER", paused_from="RESEARCH")
    with pytest.raises(M.IllegalModeTransition):
        M.assert_legal_startup("PAUSED", "PRODUCTION_ACTIVE", paused_from="RESEARCH",
                               confirmed=True)
    # DISABLED remains available regardless -- the full-reset kill switch.
    M.assert_legal_startup("PAUSED", "DISABLED", paused_from="RESEARCH")


def test_the_disabled_paused_production_active_bypass_is_closed():
    """The independently-discovered, more serious half of this fix: under
    the OLD chain-adjacency model, DISABLED -> PAUSED (unconditional) ->
    PRODUCTION_ACTIVE (merely confirmed) was a two-hop path to live trading
    from a system that had NEVER actually run in RESEARCH or PAPER --
    silently defeating the entire one-step escalation rule. paused_from
    closes it: PAUSED entered from DISABLED can only resume to DISABLED,
    confirmation or not."""
    M.assert_legal_startup("DISABLED", "PAUSED")   # entering PAUSED: still unconditional
    with pytest.raises(M.IllegalModeTransition):
        M.assert_legal_startup("PAUSED", "PRODUCTION_ACTIVE", paused_from="DISABLED",
                               confirmed=True)


def test_unknown_persisted_or_target_mode_is_a_readable_error():
    with pytest.raises(M.ModeTransitionError, match="unknown mode"):
        M.assert_legal_startup("NOT_A_MODE", "PAPER")
    with pytest.raises(M.ModeTransitionError, match="unknown mode"):
        M.assert_legal_startup("DISABLED", "NOT_A_MODE")
