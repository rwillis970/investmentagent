#!/usr/bin/env python3
"""READ-ONLY evidence inspection CLI (Track C, 2026-08-14).

Opens the real durable `agent.store.FactStore` and `agent.
opportunity_event_store.OpportunityEventStore` files and prints their
contents -- nothing else. This script never calls `.append()`/`.record()`
on either store; it only ever constructs them (which replays the existing
file into memory, exactly like every other reader of these stores already
does) and reads back what is already there.

WHY THIS EXISTS. Phase 2/3 of the overnight readiness mission asked for "a
small CLI/read-only inspection command (facts list/show, opportunities
list/show)" specifically so an operator can look at what the real pipeline
has actually persisted -- which raw facts a collection cycle wrote, and
which materiality events a screen cycle produced (triggered, suppressed,
or scored below threshold alike) -- without writing one-off Python at a
REPL every time, and without risking an accidental write to canonical
data (there is no write path in this file at all).

SUBCOMMANDS:

  facts list [--entity-id ID] [--field F] [--source-id S] [--limit N]
      Lists facts from --fact-store-path, most-recently-observed first,
      optionally filtered. Each line is one fact's summary.

  facts show <entity-id> <field>
      Prints the FULL history for one (entity_id, field) series -- every
      fact ever observed for it, oldest first (this is `AsOfView.history`
      as of "now," i.e. everything currently in the store; the whole point
      of a bitemporal store is that this is not "the current value," it is
      "every value we have ever recorded, each still exactly as true today
      as it was the day it was written").

  opportunities list [--status S] [--symbol SYM] [--type T] [--limit N]
      Lists materiality-screen events from --opportunity-event-store-path,
      durable append order, optionally filtered by `analysis_status`
      ("PENDING_ANALYSIS"/"SUPPRESSED"/"NOT_MATERIAL"), symbol, or `type`
      ("FILING"/"PRICE_MOVE").

  opportunities show <event-id>
      Prints one event's full detail: every field on `agent.entities.
      OpportunityEvent`, plus this store's own `evaluated_at` (the moment
      the screen cycle that produced this row actually ran).

`--data-dir` defaults every unspecified store path the same way `scripts/
run_agent.py` does (`<data-dir>/facts.jsonl`, `<data-dir>/
materiality_events.jsonl`) -- but neither this script nor either store
class it uses ever creates `--data-dir` if it does not already exist (a
read tool has no reason to `mkdir` anything); a missing store file is
reported as an empty result, not an error, matching `OpportunityEventStore
`'s own "file does not exist yet" constructor behavior.

OUTPUT FORMAT: one JSON object per line (JSON Lines) to stdout -- scriptable
(`| jq`, `| grep`) without this tool inventing its own table format.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from agent.opportunity_event_store import OpportunityEventStore  # noqa: E402
from agent.store import Fact, FactStore  # noqa: E402


def _fact_line(f: Fact) -> dict:
    return {
        "entity_id": f.entity_id, "field": f.field, "value": f.value,
        "observed_at": f.observed_at.isoformat(), "effective_at": f.effective_at.isoformat(),
        "source_id": f.source_id, "source_doc_hash": f.source_doc_hash,
    }


def facts_list(fact_store: FactStore, *, entity_id: str | None, field: str | None,
              source_id: str | None, limit: int | None) -> list[dict]:
    rows = list(fact_store.all_facts())
    if entity_id is not None:
        rows = [f for f in rows if f.entity_id == entity_id]
    if field is not None:
        rows = [f for f in rows if f.field == field]
    if source_id is not None:
        rows = [f for f in rows if f.source_id == source_id]
    rows.sort(key=lambda f: f.observed_at, reverse=True)
    if limit is not None:
        rows = rows[:limit]
    return [_fact_line(f) for f in rows]


def facts_show(fact_store: FactStore, *, entity_id: str, field: str) -> list[dict]:
    view = fact_store.now_view()
    history = view.history(entity_id, field)
    return [_fact_line(f) for f in history]


def _event_line(store: OpportunityEventStore, e) -> dict:
    return {
        "event_id": e.event_id, "type": e.type, "source_id": e.source_id,
        "symbols": list(e.symbols), "materiality_score": e.materiality_score,
        "threshold_version": e.threshold_version, "analysis_status": e.analysis_status,
        "suppressed_reason": e.suppressed_reason,
        "observed_at": e.observed_at.isoformat(), "effective_at": e.effective_at.isoformat(),
        "evaluated_at": store.evaluated_at(e.event_id),
    }


def _event_detail(store: OpportunityEventStore, e) -> dict:
    row = _event_line(store, e)
    row["score_components"] = e.score_components
    return row


def opportunities_list(store: OpportunityEventStore, *, status: str | None,
                       symbol: str | None, event_type: str | None,
                       limit: int | None) -> list[dict]:
    rows = list(store.all())
    if status is not None:
        rows = [e for e in rows if e.analysis_status == status]
    if symbol is not None:
        rows = [e for e in rows if symbol in e.symbols]
    if event_type is not None:
        rows = [e for e in rows if e.type == event_type]
    if limit is not None:
        rows = rows[-limit:]   # most recently APPENDED last -- tail = most recent
    return [_event_line(store, e) for e in rows]


def opportunities_show(store: OpportunityEventStore, *, event_id: str) -> dict | None:
    e = store.get(event_id)
    if e is None:
        return None
    return _event_detail(store, e)


def _print_jsonl(rows) -> None:
    if isinstance(rows, list):
        for row in rows:
            print(json.dumps(row, default=str))
    else:
        print(json.dumps(rows, default=str))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data-dir", default=None,
                        help="defaults --fact-store-path/--opportunity-event-store-path to "
                             "<data-dir>/facts.jsonl and <data-dir>/materiality_events.jsonl "
                             "when either is not given explicitly. Never created if missing "
                             "(this is a read-only tool).")
    parser.add_argument("--fact-store-path", default=None)
    parser.add_argument("--opportunity-event-store-path", default=None)

    sub = parser.add_subparsers(dest="command", required=True)

    facts_p = sub.add_parser("facts", help="inspect agent.store.FactStore")
    facts_sub = facts_p.add_subparsers(dest="facts_command", required=True)
    fl = facts_sub.add_parser("list")
    fl.add_argument("--entity-id", default=None)
    fl.add_argument("--field", default=None)
    fl.add_argument("--source-id", default=None)
    fl.add_argument("--limit", type=int, default=None)
    fs = facts_sub.add_parser("show")
    fs.add_argument("entity_id")
    fs.add_argument("field")

    opp_p = sub.add_parser("opportunities",
                           help="inspect agent.opportunity_event_store.OpportunityEventStore")
    opp_sub = opp_p.add_subparsers(dest="opportunities_command", required=True)
    ol = opp_sub.add_parser("list")
    ol.add_argument("--status", default=None,
                    choices=["PENDING_ANALYSIS", "SUPPRESSED", "NOT_MATERIAL"])
    ol.add_argument("--symbol", default=None)
    ol.add_argument("--type", dest="event_type", default=None)
    ol.add_argument("--limit", type=int, default=None)
    os_ = opp_sub.add_parser("show")
    os_.add_argument("event_id")

    args = parser.parse_args(argv)

    fact_store_path = args.fact_store_path
    opp_store_path = args.opportunity_event_store_path
    if args.data_dir is not None:
        data_dir = Path(args.data_dir)
        if fact_store_path is None:
            fact_store_path = data_dir / "facts.jsonl"
        if opp_store_path is None:
            opp_store_path = data_dir / "materiality_events.jsonl"

    if args.command == "facts":
        if fact_store_path is None:
            parser.error("facts requires --fact-store-path or --data-dir")
        fact_store = FactStore(fact_store_path)
        if args.facts_command == "list":
            _print_jsonl(facts_list(fact_store, entity_id=args.entity_id, field=args.field,
                                    source_id=args.source_id, limit=args.limit))
        else:
            rows = facts_show(fact_store, entity_id=args.entity_id, field=args.field)
            if not rows:
                print(f"no facts for ({args.entity_id!r}, {args.field!r})", file=sys.stderr)
                return 1
            _print_jsonl(rows)
        return 0

    if args.command == "opportunities":
        if opp_store_path is None:
            parser.error("opportunities requires --opportunity-event-store-path or --data-dir")
        store = OpportunityEventStore(opp_store_path)
        if args.opportunities_command == "list":
            _print_jsonl(opportunities_list(store, status=args.status, symbol=args.symbol,
                                            event_type=args.event_type, limit=args.limit))
        else:
            row = opportunities_show(store, event_id=args.event_id)
            if row is None:
                print(f"no event with event_id={args.event_id!r}", file=sys.stderr)
                return 1
            _print_jsonl(row)
        return 0

    return 1   # unreachable -- argparse's own required=True on `sub` prevents this


if __name__ == "__main__":
    sys.exit(main())
