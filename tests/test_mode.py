"""Mode transition guard (§9.2, §12 criterion 3, Day-1 exit criterion).

Before this, `mode` was validated only for tuple membership -- nothing
checked that the mode named in a *loaded* config was actually reachable
from where the system last was. That is why "DISABLED to PRODUCTION_ACTIVE
in one step is impossible" passed trivially: nothing asserted it at all.

    DISABLED <-> RESEARCH <-> PAPER <-> PRODUCTION_ACTIVE <-> PAUSED

Forward movement is one step at a time. DISABLED and PAUSED are reachable
immediately and unconditionally from any state -- the kill-switch ends of
the chain. Both edges into PRODUCTION_ACTIVE -- from PAPER and from PAUSED --
additionally require explicit confirmation (real re-authentication against
live credentials is a Day-10 concern and out of scope here; this module only
enforces that neither edge can be crossed silently by config alone). PAUSED
itself stays reachable unconditionally: only resuming OUT of it into live
trading is gated, never entering it.
"""
import pytest

from agent import mode as M


# -- is_legal_step: the chain, ignoring confirmation ------------------------

def test_adjacent_forward_steps_are_legal():
    assert M.is_legal_step("DISABLED", "RESEARCH")
    assert M.is_legal_step("RESEARCH", "PAPER")
    assert M.is_legal_step("PAPER", "PRODUCTION_ACTIVE")
    assert M.is_legal_step("PRODUCTION_ACTIVE", "PAUSED")


def test_adjacent_backward_steps_are_legal():
    assert M.is_legal_step("RESEARCH", "DISABLED")
    assert M.is_legal_step("PAPER", "RESEARCH")
    assert M.is_legal_step("PRODUCTION_ACTIVE", "PAPER")
    assert M.is_legal_step("PAUSED", "PRODUCTION_ACTIVE")


def test_same_mode_is_always_legal():
    for m in M.CHAIN:
        assert M.is_legal_step(m, m)


def test_multi_step_jump_is_illegal():
    assert not M.is_legal_step("DISABLED", "PAPER")
    assert not M.is_legal_step("DISABLED", "PRODUCTION_ACTIVE")
    assert not M.is_legal_step("RESEARCH", "PRODUCTION_ACTIVE")
    # Not DISABLED or PAUSED targets, so the immediate-target exception does
    # not apply and the ordinary one-step rule governs.
    assert not M.is_legal_step("PRODUCTION_ACTIVE", "RESEARCH")
    assert not M.is_legal_step("PAUSED", "RESEARCH")


def test_disabled_and_paused_are_reachable_from_any_state_unconditionally():
    """The kill-switch property: even though PAUSED is three steps from
    DISABLED on the chain, and vice versa, both are always one legal step
    away from anywhere."""
    for start in M.CHAIN:
        assert M.is_legal_step(start, "DISABLED")
        assert M.is_legal_step(start, "PAUSED")


def test_unknown_mode_is_rejected_not_silently_false():
    with pytest.raises(M.ModeTransitionError):
        M.is_legal_step("DISABLED", "SUSPENDED")
    with pytest.raises(M.ModeTransitionError):
        M.is_legal_step("SUSPENDED", "DISABLED")


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
    re-authentication §9.2 requires of the initial PAPER promotion --
    resuming after any pause, including an operator-initiated one, is
    exactly the moment confirmation matters most."""
    with pytest.raises(M.ConfirmationRequired):
        M.assert_legal_startup("PAUSED", "PRODUCTION_ACTIVE")
    with pytest.raises(M.ConfirmationRequired):
        M.assert_legal_startup("PAUSED", "PRODUCTION_ACTIVE", confirmed=False)
    # Does not raise when confirmed.
    M.assert_legal_startup("PAUSED", "PRODUCTION_ACTIVE", confirmed=True)


def test_confirmation_is_required_on_both_edges_into_production_active_and_nowhere_else():
    """Every other legal edge -- including the reverse of either guarded
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


def test_pause_and_resume_round_trip():
    """Pausing needs no confirmation -- PAUSED is a kill-switch target.
    Resuming from it back into PRODUCTION_ACTIVE does, same as the initial
    PAPER -> PRODUCTION_ACTIVE promotion."""
    M.assert_legal_startup("PRODUCTION_ACTIVE", "PAUSED")
    with pytest.raises(M.ConfirmationRequired):
        M.assert_legal_startup("PAUSED", "PRODUCTION_ACTIVE")
    M.assert_legal_startup("PAUSED", "PRODUCTION_ACTIVE", confirmed=True)


def test_unknown_persisted_or_target_mode_is_a_readable_error():
    with pytest.raises(M.ModeTransitionError, match="unknown mode"):
        M.assert_legal_startup("NOT_A_MODE", "PAPER")
    with pytest.raises(M.ModeTransitionError, match="unknown mode"):
        M.assert_legal_startup("DISABLED", "NOT_A_MODE")
