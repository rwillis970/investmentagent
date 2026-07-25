"""Keep the Python entities and the SQL schema in step (§9.1).

Nothing else enforces this, so a column added to one and not the other fails
here rather than at 09:30 on a Tuesday.
"""
import pathlib
import re
from dataclasses import fields

import pytest

from agent import entities as E

SQL = (pathlib.Path(__file__).resolve().parent.parent
       / "migrations" / "001_init.sql").read_text()

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


def sql_columns(table: str) -> set[str]:
    m = re.search(r"CREATE TABLE " + re.escape(table) + r"\s*\((.*?)\n\);",
                  SQL, re.S)
    assert m, f"table {table} not found in migrations/001_init.sql"
    cols = set()
    for line in m.group(1).splitlines():
        line = line.strip()
        if not line or line.startswith("--"):
            continue
        first = line.split()[0]
        if first.upper() in SKIP_FIRST_TOKEN:
            continue
        cols.add(first.strip('",').lower())
    return cols


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
    kw = dict(run_id="r", as_of=datetime.now(timezone.utc), mode="PAPER",
              code_commit="abc", cadence_config_version="1",
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
