"""Shared fixture: a known-valid raw config dict for tests that need to
call `agent.config.load`/`validate` directly (operator decision surface
unit, 2026-08-03) -- built from the repository's own `config.example.json`
(the canonical, already-valid reference) rather than a second, independently
hand-maintained dict that could drift from it.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_EXAMPLE_PATH = Path(__file__).resolve().parent.parent / "config.example.json"


def valid_raw_config(**overrides: Any) -> dict:
    raw = json.loads(_EXAMPLE_PATH.read_text(encoding="utf-8"))
    raw.update(overrides)
    return raw
