"""agent/dashboard_config.py (operator decision surface unit, 2026-08-03):
the three-class config-write gate. See that module's own docstring for why
classification is a table in code, never derived from the request.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from agent.audit import AuditLog
from agent.dashboard_config import (FREELY_WRITABLE, NOT_WRITABLE,
                                    RE_AUTH_REQUIRED, apply_config_patch,
                                    classify)
from tests.test_config_fixture import valid_raw_config

NOW = datetime(2026, 8, 3, 15, 0, tzinfo=timezone.utc)


def write_config(tmp_path, **overrides):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(valid_raw_config(**overrides)))
    return path


# ------------------------------------------------------------- classify()

def test_mode_is_never_writable():
    assert classify("mode") == NOT_WRITABLE


def test_an_unknown_field_name_is_not_writable_by_default():
    assert classify("this_field_does_not_exist") == NOT_WRITABLE


def test_the_operators_named_re_auth_fields_are_all_re_auth_required():
    for field in (
        "minimum_settled_cash_pct_of_nlv", "max_position_pct", "price_band_pct",
        "budget_hard_stop_usd", "t4_analysis_enabled",
        "approval_min_display_seconds", "max_approval_requests_per_day",
    ):
        assert classify(field) == RE_AUTH_REQUIRED, field


def test_an_ordinary_cadence_field_is_freely_writable():
    assert classify("opportunity_screen_interval_minutes") == FREELY_WRITABLE
    assert classify("data_collection_interval_seconds") == FREELY_WRITABLE


def test_demoted_throttle_fields_are_freely_writable_not_re_auth(tmp_path):
    """Review follow-up, 2026-08-03: adding a symbol does not widen a risk
    gate, and a new-position cap / cooldown period are throttles whose
    worst case is fewer opportunities, not more exposure or less friction --
    these three no longer belong in RE_AUTH_REQUIRED_FIELDS."""
    for field in ("symbol_universe", "max_new_positions_per_day",
                 "trade_cooldown_period"):
        assert classify(field) == FREELY_WRITABLE, field


def test_remaining_pdt_and_holding_fields_stay_re_auth_required():
    for field in ("max_day_trades_per_5_sessions", "drawdown_pause_pct",
                 "minimum_holding_period", "risk_profile",
                 "approval_expiration_minutes"):
        assert classify(field) == RE_AUTH_REQUIRED, field


def test_capability_tables_are_not_writable_from_this_surface():
    for field in ("trade_capabilities", "sides", "funding", "order_types",
                 "sessions", "time_in_force"):
        assert classify(field) == NOT_WRITABLE, field


def test_require_human_trade_approval_is_not_writable():
    assert classify("require_human_trade_approval") == NOT_WRITABLE


# ------------------------------------------------------- apply_config_patch

def test_a_freely_writable_field_is_accepted_without_confirmation(tmp_path):
    path = write_config(tmp_path)
    audit = AuditLog()
    result = apply_config_patch(
        config_path=path, key="opportunity_screen_interval_minutes", value=10,
        confirmed=False, actor="ray", audit_log=audit, now=NOW,
    )
    assert result["accepted"] is True
    assert result["config"].opportunity_screen_interval_minutes == 10
    written = json.loads(path.read_text())
    assert written["opportunity_screen_interval_minutes"] == 10


def test_a_re_auth_field_without_confirmed_is_refused_and_not_written(tmp_path):
    path = write_config(tmp_path)
    before = path.read_text()
    audit = AuditLog()
    result = apply_config_patch(
        config_path=path, key="max_position_pct", value=9.0, confirmed=False,
        actor="ray", audit_log=audit, now=NOW,
    )
    assert result["accepted"] is False
    assert "re-authentication" in result["reason"]
    assert path.read_text() == before   # nothing written


def test_a_re_auth_field_with_confirmed_true_is_accepted(tmp_path):
    path = write_config(tmp_path)
    audit = AuditLog()
    result = apply_config_patch(
        config_path=path, key="max_position_pct", value=9.0, confirmed=True,
        actor="ray", audit_log=audit, now=NOW,
    )
    assert result["accepted"] is True
    assert result["config"].max_position_pct == 9.0


def test_a_not_writable_field_is_refused_even_with_confirmed_true(tmp_path):
    path = write_config(tmp_path)
    audit = AuditLog()
    result = apply_config_patch(
        config_path=path, key="mode", value="PRODUCTION_ACTIVE", confirmed=True,
        actor="ray", audit_log=audit, now=NOW,
    )
    assert result["accepted"] is False
    assert "not writable" in result["reason"]


def test_a_value_that_fails_validate_is_refused_and_not_written(tmp_path):
    path = write_config(tmp_path)
    before = path.read_text()
    audit = AuditLog()
    # max_position_pct above MAX_POSITION_CEILING (15.0) fails agent.config.validate.
    result = apply_config_patch(
        config_path=path, key="max_position_pct", value=99.0, confirmed=True,
        actor="ray", audit_log=audit, now=NOW,
    )
    assert result["accepted"] is False
    assert "agent.config.validate" in result["reason"]
    assert path.read_text() == before


def test_an_unknown_key_is_refused(tmp_path):
    path = write_config(tmp_path)
    audit = AuditLog()
    result = apply_config_patch(
        config_path=path, key="not_a_real_field", value=1, confirmed=True,
        actor="ray", audit_log=audit, now=NOW,
    )
    assert result["accepted"] is False
    assert "not a known config field" in result["reason"]


def test_every_accepted_write_is_audited_with_old_and_new_value(tmp_path):
    path = write_config(tmp_path)
    audit = AuditLog()
    apply_config_patch(config_path=path, key="opportunity_screen_interval_minutes",
                       value=10, confirmed=False, actor="ray", audit_log=audit, now=NOW)
    events = audit.events
    assert len(events) == 1
    assert events[0].action == "config_write_accepted"
    assert events[0].actor == "ray"
    assert events[0].after["value"] == 10


def test_every_rejected_write_is_also_audited(tmp_path):
    path = write_config(tmp_path)
    audit = AuditLog()
    apply_config_patch(config_path=path, key="mode", value="PRODUCTION_ACTIVE",
                       confirmed=True, actor="ray", audit_log=audit, now=NOW)
    events = audit.events
    assert len(events) == 1
    assert events[0].action == "config_write_rejected"
    assert events[0].actor == "ray"


def test_a_replayed_read_after_write_reflects_the_new_value(tmp_path):
    """A second patch call reads the FILE it just wrote, not a stale
    in-memory copy -- confirms the read-modify-write path round-trips."""
    path = write_config(tmp_path)
    audit = AuditLog()
    apply_config_patch(config_path=path, key="opportunity_screen_interval_minutes",
                       value=10, confirmed=False, actor="ray", audit_log=audit, now=NOW)
    result = apply_config_patch(config_path=path, key="materiality_threshold",
                                value=3.0, confirmed=False, actor="ray",
                                audit_log=audit, now=NOW)
    assert result["config"].opportunity_screen_interval_minutes == 10
    assert result["config"].materiality_threshold == 3.0
