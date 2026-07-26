"""Keep the Python entities and the SQL schema in step (§9.1).

Nothing else enforces this, so a column added to one and not the other fails
here rather than at 09:30 on a Tuesday.

Reads ALL migrations in numeric order and applies CREATE TABLE / ALTER TABLE
... ADD COLUMN in sequence, so a column added via a later migration (e.g.
002_multi_account.sql's account_id additions) counts toward the schema a
Python entity is compared against -- the same way a real migration runner
would leave the database.
"""
import pathlib
import re
from dataclasses import fields

import pytest

from agent import entities as E

MIGRATIONS_DIR = pathlib.Path(__file__).resolve().parent.parent / "migrations"

SKIP_FIRST_TOKEN = {"CONSTRAINT", "PRIMARY", "UNIQUE", "CHECK", "FOREIGN"}

# Python name -> SQL name, where they legitimately differ
ALIASES = {"klass": "class"}

CASES = [
    (E.OpportunityEvent, "agent.opportunity_event"),
    (E.ApprovalRequest, "agent.approval_request"),
    (E.RunManifest, "agent.run_manifest"),
    (E.CapabilityChangeRequest, "policy.capability_change_request"),
    (E.PlaybookCandidate, "agent.playbook_candidate"),
]


def _build_schema() -> dict[str, set[str]]:
    """Apply every migration in numeric order, tracking each table's column
    set through CREATE TABLE and ALTER TABLE ... ADD COLUMN statements."""
    schema: dict[str, set[str]] = {}
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        sql = path.read_text()

        for m in re.finditer(r'CREATE TABLE (\S+)\s*\((.*?)\n\);', sql, re.S):
            table, body = m.group(1), m.group(2)
            cols = set()
            for line in body.splitlines():
                line = line.strip()
                if not line or line.startswith("--"):
                    continue
                first = line.split()[0]
                if first.upper() in SKIP_FIRST_TOKEN:
                    continue
                cols.add(first.strip('",').lower())
            schema[table] = cols

        for m in re.finditer(
                r'ALTER TABLE (\S+)\s+ADD COLUMN (\S+)', sql):
            table, col = m.group(1), m.group(2)
            schema.setdefault(table, set()).add(col.strip('",').lower())

    return schema


SCHEMA = _build_schema()


def sql_columns(table: str) -> set[str]:
    assert table in SCHEMA, f"table {table} not found in any migration"
    return SCHEMA[table]


@pytest.mark.parametrize("cls,table", CASES, ids=[c[1] for c in CASES])
def test_entity_fields_match_sql_columns(cls, table):
    py = {ALIASES.get(f.name, f.name) for f in fields(cls)}
    sql = sql_columns(table)
    assert py == sql, (
        f"{cls.__name__} vs {table}\n"
        f"  only in Python: {sorted(py - sql)}\n"
        f"  only in SQL:    {sorted(sql - py)}"
    )


def test_run_manifest_rejects_an_unknown_trigger():
    from datetime import datetime, timezone
    kw = dict(run_id="r", account_id="acct-taxable", as_of=datetime.now(timezone.utc),
              mode="PAPER", code_commit="abc", cadence_config_version="1",
              holding_policy_version="1", capability_policy_version="1",
              risk_policy_version="1", playbook_version="1",
              threshold_version="1", prompt_versions=(), model_ids=(),
              store_watermark=datetime.now(timezone.utc))
    E.RunManifest(trigger="EVENT", **kw)
    with pytest.raises(ValueError, match="unknown trigger"):
        E.RunManifest(trigger="WHENEVER", **kw)


def test_playbook_candidate_class_must_be_a_or_b():
    from datetime import datetime, timezone
    kw = dict(candidate_id="c", parent_version="p", change_set={},
              hypothesis="h", decision_rule="d",
              registered_at=datetime.now(timezone.utc))
    E.PlaybookCandidate(klass="A", **kw)
    with pytest.raises(ValueError):
        E.PlaybookCandidate(klass="C", **kw)
