"""Operator dashboard: GET /api/state assembly (operator decision surface
unit, 2026-08-03). Read-only. One JSON document, assembled from the real
stores this codebase already has -- never a fabricated number.

HONESTY, NOT COMPLETENESS. The uploaded design (`Agent Command Center.dc.html`)
renders far more panels than this codebase has real, queryable state behind:
per-session collector counts, materiality-screen scored/suppressed/triggered
counts, a persisted reconciliation-result history, an improvement loop, a
performance-attribution layer. None of those exist as a durable, queryable
store anywhere in this codebase (checked directly, not assumed -- see this
unit's own report for the full list). Rather than inventing a plausible
number, every such field is returned as an explicit `null`, with a sibling
`<field>_unavailable_reason` string explaining why -- the design itself
already renders "NOT BUILT"/"NO LABELS"/"ABSENT" states for exactly this
reason, and this endpoint must keep telling the truth, not paper over the
gap with a fake number the moment a real backend exists to ask.

NO BROKER CALL, NO CREDENTIAL. This module never constructs a `BrokerAdapter`
or touches `agent.secrets_provider` -- `broker_account`/`broker_positions`/
`ledger`/`day_trade_guard`, when available, are supplied by the caller
(mirroring `agent.pipeline_stage.run_pipeline_stage`'s own "first reconciled
account's state, not fetched here" convention) and are optional: every
section that needs one degrades to null+reason when it is not supplied,
rather than reaching for a broker connection itself.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from . import market_calendar
from .accounts import AccountType
from .approval import ApprovalService
from .approval_request_store import ApprovalRequestStore
from .audit import AuditLog
from .broker.base import AccountSnapshot, Position
from .config import Config
from .cost import CostLedger
from .daytrade import DayTradeGuard
from .ledger import Ledger
from .opportunity_event_tracker import OpportunityEventTracker
from .risk import PortfolioState, investable_cash, required_reserve

_NOT_BUILT = (
    "not built: no code path or durable store exists for this figure in "
    "this codebase yet"
)
_NO_SESSION_HISTORY = (
    "not available: this figure is computed in-memory for a single "
    "pipeline cycle and is never persisted anywhere queryable afterward"
)
_NO_RECONCILE_HISTORY = (
    "not available: agent.reconciliation exposes pure comparison functions "
    "only (reconcile_settled_cash/positions/open_orders) -- there is no "
    "persisted 'last reconciliation result' store to read from"
)


def _null(reason: str) -> dict:
    return {"value": None, "unavailable_reason": reason}


def _present(value: Any) -> dict:
    return {"value": value, "unavailable_reason": None}


def build_dashboard_state(
    *, now: datetime, config: Config, cost_ledger: CostLedger,
    opportunity_tracker: OpportunityEventTracker,
    approval_request_store: ApprovalRequestStore, audit_log: AuditLog,
    account_id: str | None = None, approval_service: ApprovalService | None = None,
    broker_account: AccountSnapshot | None = None,
    broker_positions: tuple[Position, ...] = (),
    day_trade_guard: DayTradeGuard | None = None,
    ledger: Ledger | None = None,
    audit_recent_limit: int = 20,
) -> dict:
    """Assemble the single JSON document the dashboard's GET /api/state
    returns. Every store this function reads is passed in by the caller
    (`agent.dashboard_server`, wired to the real, running process's own
    stores) -- this function itself opens nothing and constructs no
    collaborator, so it is directly unit-testable against fakes/fixtures."""
    today = now.date()
    session = market_calendar.session_for_instant(now)

    cost_state = cost_ledger.state(today)
    cost = {
        "month_to_date_usd": cost_ledger.month_to_date(today),
        "monthly_budget_usd": config.monthly_budget_usd,
        "budget_warning_usd": config.budget_warning_usd,
        "budget_hard_stop_usd": config.budget_hard_stop_usd,
        "budget_state": cost_state.value,
        "analyses_today": cost_ledger.analyses_today(today),
        "max_model_analyses_per_day": config.max_model_analyses_per_day,
        "cache_hit_rate": cost_ledger.cache_hit_rate(),
        "t4_input_price_per_million_tokens": config.t4_input_price_per_million_tokens,
        "t4_output_price_per_million_tokens": config.t4_output_price_per_million_tokens,
    }

    data_collection = {
        "enabled": config.data_collection_enabled,
        "interval_seconds": config.data_collection_interval_seconds,
        **_prefixed("bars_ingested_today", _null(_NOT_BUILT)),
        **_prefixed("filings_ingested_today", _null(_NOT_BUILT)),
        **_prefixed("news_feed", _null(
            "not built: no news collector exists anywhere in this codebase"
        )),
    }

    materiality_screen = {
        "enabled": config.materiality_screen_enabled,
        "threshold": config.materiality_threshold,
        "threshold_version": config.threshold_version,
        "interval_seconds": config.opportunity_screen_interval_minutes * 60,
        **_prefixed("scored_this_session", _null(_NO_SESSION_HISTORY)),
        **_prefixed("suppressed_this_session", _null(_NO_SESSION_HISTORY)),
        **_prefixed("triggered_this_session", _null(_NO_SESSION_HISTORY)),
    }

    analysis = {
        "enabled": config.t4_analysis_enabled,
        "model_id": config.t4_model_id,
        **_prefixed("currently_analyzing", _null(_NO_SESSION_HISTORY)),
        **_prefixed("bear_bull_contra_counts_this_session", _null(_NO_SESSION_HISTORY)),
    }

    risk_gates = {
        "max_position_pct": config.max_position_pct,
        "max_sector_pct": config.max_sector_pct,
        "minimum_settled_cash_pct_of_nlv": config.minimum_settled_cash_pct_of_nlv,
        "minimum_absolute_settled_cash": config.minimum_absolute_settled_cash,
    }
    if broker_account is not None:
        portfolio = PortfolioState(
            account_id=account_id or broker_account.account_id,
            nlv=float(broker_account.equity),
            settled_cash=float(broker_account.settled_cash),
            unsettled_cash=float(broker_account.unsettled_cash),
        )
        floor = required_reserve(portfolio, config.risk_policy)
        available = investable_cash(portfolio, config.risk_policy)
        reserve_pct = (float(broker_account.settled_cash) / portfolio.nlv * 100.0
                      if portfolio.nlv else 0.0)
        risk_gates.update({
            **_prefixed("current_reserve_pct", _present(reserve_pct)),
            **_prefixed("required_reserve_usd", _present(floor)),
            **_prefixed("investable_cash_usd", _present(available)),
            # DASHBOARD FIX (2026-08-12): same source as current_reserve_pct
            # above (`broker_account.settled_cash`/`.unsettled_cash`) -- the
            # dashboard's own "Capital"/"Settled cash" figures were reading
            # hardcoded sample values instead of these.
            **_prefixed("settled_cash_usd", _present(float(broker_account.settled_cash))),
            **_prefixed("unsettled_cash_usd", _present(float(broker_account.unsettled_cash))),
            # DASHBOARD FIX follow-up (2026-08-12): agent_command_center.html's
            # footer "CAPITAL" figure is the account's total equity/NLV, not
            # settled cash -- the same `portfolio.nlv` this function already
            # computes reserve_pct against above, just never previously
            # exposed as its own field. Genuinely new (there was no existing
            # risk_gates field this could reuse without misrepresenting it).
            **_prefixed("nlv_usd", _present(float(broker_account.equity))),
        })
    else:
        for name in ("current_reserve_pct", "required_reserve_usd", "investable_cash_usd",
                     "settled_cash_usd", "unsettled_cash_usd", "nlv_usd"):
            risk_gates.update(_prefixed(name, _null(
                "no broker_account was supplied to build_dashboard_state for this cycle"
            )))

    # DASHBOARD FIX (2026-08-12): `broker_positions` is a separate parameter
    # from `broker_account` (its own default of `()`, never None) -- so
    # unlike settled_cash_usd/unsettled_cash_usd above, this is always a
    # real, present list (possibly empty), never null/unavailable.
    risk_gates.update(_prefixed("broker_positions", _present([
        {"symbol": p.symbol, "qty": float(p.qty), "market_value": float(p.market_value)}
        for p in broker_positions
    ])))

    pending = approval_request_store.pending(account_id=account_id, now=now)
    pending_out = []
    for req in sorted(pending, key=lambda r: r.expires_at):
        proposal = req.proposal_snapshot or {}
        pending_out.append({
            "request_id": req.request_id,
            "symbol": proposal.get("symbol"),
            "side": proposal.get("side"),
            "authorized_qty": proposal.get("authorized_qty"),
            "limit_price": proposal.get("limit_price"),
            "earmark": req.earmark,
            "shown_at": req.shown_at.isoformat(),
            "expires_at": req.expires_at.isoformat(),
            "seconds_until_expiry": max(0.0, (req.expires_at - now).total_seconds()),
            "confidence": proposal.get("confidence"),
        })
    outstanding = (approval_request_store.outstanding_earmarks(
        account_id, now, service=approval_service) if account_id is not None else None)
    approvals = {
        "enabled": config.approval_request_enabled,
        "pending": pending_out,
        # No deferred-approval mechanism exists anywhere in this codebase
        # (checked directly, not assumed -- see this unit's own report).
        # Always empty until one is actually built; shaped as a list of
        # {proposal_snapshot, reason} dicts, mirroring `pending`'s own
        # per-request dict shape, so the frontend contract is stable the
        # day a real mechanism starts populating it.
        "deferred": [],
        "decided_today": approval_request_store.count_decided_on(session),
        "max_approval_requests_per_day": config.max_approval_requests_per_day,
        "approval_min_display_seconds": config.approval_min_display_seconds,
        "approval_expiration_minutes": config.approval_expiration_minutes,
        "price_band_pct": config.price_band_pct,
        **_prefixed("outstanding_earmarks_usd",
                   _present(outstanding) if outstanding is not None else
                   _null("no account_id was supplied to build_dashboard_state")),
    }

    reconciliation = {
        "cycle_interval_seconds": config.reconciliation_cycle_interval_seconds,
        **_prefixed("last_result", _null(_NO_RECONCILE_HISTORY)),
        **_prefixed(
            "day_trade_count",
            _present(day_trade_guard.count(session)) if day_trade_guard is not None
            else _null("no day_trade_guard was supplied to build_dashboard_state"),
        ),
        **_prefixed("day_trade_count_broker_verified", _null(
            "no persisted reconciliation-result history exists to confirm this "
            "session's local count against the broker's own"
        )),
    }

    recent_audit = [
        {"seq": ev.seq, "actor": ev.actor, "action": ev.action,
        "object_type": ev.object_type, "object_id": ev.object_id,
        "timestamp": ev.timestamp.isoformat()}
        for ev in list(audit_log.events)[-audit_recent_limit:]
    ]
    audit = {
        "hash_chain_verified": audit_log.verify(),
        "recent": recent_audit,
        "truncated_tail_on_load": audit_log.truncated_tail_on_load,
    }

    improvement_loop = {
        "enabled": False,
        **_prefixed("class_a_reading_quality_labels", _null(_NOT_BUILT)),
        **_prefixed("class_b_decision_quality_history", _null(_NOT_BUILT)),
        **_prefixed("sessions_recorded", _null(_NOT_BUILT)),
        "note": "§12: no code path exists behind an improvement loop yet",
    }
    # Performance-plumbing unit (2026-08-13): `closed_positions`/
    # `realized_pnl_usd` are now real, computed from `ledger.closed_lots()`
    # (agent/ledger.py -- reconstructed from the fill log, which never
    # discards a fill; see that method's own docstring). `attribution`
    # stays permanently _NOT_BUILT here -- a benchmark-relative, tax-aware,
    # since-inception comparison is a genuinely separate, much larger
    # feature this unit's scope does not cover (see the panel's own design
    # copy: "the highest-value thing left to build"). Zero closed lots is
    # a real, honest, present value (0), NOT the same as "not built" --
    # the two must not be conflated: a fresh account with no sells yet
    # correctly reports closed_positions=0, not null.
    if ledger is not None:
        closed = ledger.closed_lots()
        realized_pnl = sum((lot.realized_pnl for lot in closed), start=Decimal("0"))
        performance = {
            **_prefixed("closed_positions", _present(len(closed))),
            **_prefixed("realized_pnl_usd", _present(float(realized_pnl))),
            **_prefixed("attribution", _null(_NOT_BUILT)),
            "note": "§13: attribution vs. a benchmark is not implemented; "
                    "closed_positions/realized_pnl_usd are real, computed from "
                    "the ledger's own fill log",
        }
    else:
        performance = {
            **_prefixed("closed_positions", _null(
                "no ledger was supplied to build_dashboard_state for this cycle"
            )),
            **_prefixed("realized_pnl_usd", _null(
                "no ledger was supplied to build_dashboard_state for this cycle"
            )),
            **_prefixed("attribution", _null(_NOT_BUILT)),
            "note": "§13: not implemented; nothing to evaluate against a benchmark yet",
        }

    return {
        "generated_at": now.isoformat(),
        "mode": config.mode,
        "session": session.isoformat(),
        "cost": cost,
        "data_collection": data_collection,
        "materiality_screen": materiality_screen,
        "analysis": analysis,
        "risk_gates": risk_gates,
        "approvals": approvals,
        "reconciliation": reconciliation,
        "audit": audit,
        "improvement_loop": improvement_loop,
        "performance": performance,
    }


def _prefixed(name: str, pair: dict) -> dict:
    """`{"value": v, "unavailable_reason": r}` -> `{name: v, name +
    "_unavailable_reason": r}` -- the literal sibling-keys shape item 1
    asks for, kept as a small helper so every field above is built the same
    way rather than each call site hand-rolling the two keys."""
    return {name: pair["value"], f"{name}_unavailable_reason": pair["unavailable_reason"]}
