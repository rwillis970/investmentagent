"""Throwaway diagnostic (not part of the app) -- reproduces
scripts.run_dashboard._build_broker_state's exact body, real credentials,
real adapter, real data/ files, WITHOUT the try/except that normally
swallows any failure into a silent null triple. Delete this file once the
real cause is found; it exists only to surface the exception the dashboard
itself is designed to hide.

Usage: python3 scripts/diagnose_broker_state.py YOUR_KEY_ID
"""
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent import config as config_module
from agent.account_wiring import build_account_reconciliation
from agent.accounts import BrokerCredentials
from agent.broker.selection import select_broker_adapter
from agent.execution_quarantine import ExecutionQuarantineStore
from agent.holding import HoldingPolicy, HoldingPolicyRegistry
from agent.ledger_store import LedgerStore
from agent.daytrade import DayTradeGuard
from agent.secrets_provider import KeychainSecretsProvider

import json

KEY_ID = sys.argv[1] if len(sys.argv) > 1 else "REPLACE_ME"
ACCOUNT_ID = "PA3XZX944LRR"
SECRET_REF = "alpaca_api_secret"

cfg = config_module.load(json.loads(Path("config.json").read_text()))
now = datetime.now(timezone.utc)

credentials = BrokerCredentials(account_id=ACCOUNT_ID, key_id=KEY_ID, secret_ref=SECRET_REF)
secrets_provider = KeychainSecretsProvider(cfg.mode)

print("cfg.broker =", cfg.broker, " cfg.mode =", cfg.mode)

registry = HoldingPolicyRegistry([
    HoldingPolicy(version="config", minimum_holding_period=cfg.minimum_hold,
                 cooldown_period=cfg.cooldown),
])
adapter = select_broker_adapter(
    cfg, account_id=ACCOUNT_ID, credentials=credentials,
    secrets_provider=secrets_provider, now=now, transport=None,
)
print("adapter constructed:", type(adapter).__name__)

store = LedgerStore("data/ledger.jsonl", account_id=ACCOUNT_ID, policy_registry=registry)
print("LedgerStore constructed")

guard = DayTradeGuard(account_id=ACCOUNT_ID, max_per_5_sessions=cfg.max_day_trades_per_5_sessions)
print("DayTradeGuard constructed")

quarantine = ExecutionQuarantineStore("data/quarantine.jsonl", account_id=ACCOUNT_ID)
print("ExecutionQuarantineStore constructed, pending_count =", quarantine.pending_count())

recon = build_account_reconciliation(
    account_id=ACCOUNT_ID, adapter=adapter, store=store,
    day_trade_guard=guard, execution_quarantine=quarantine, now=now,
)
print("SUCCESS:")
print(recon)
