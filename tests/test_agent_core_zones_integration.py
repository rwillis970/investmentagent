"""Agent core + energy connector package integration (2026-08-03).

CLAUDE_INTEGRATION.md ships `agent-core-zones.js` (a WebGL2 custom element)
and `energy-connectors.js` (an SVG spark-particle engine) to replace the
flat orb and dashed connector animation inside `agent_command_center.html`,
preserving every existing card, layout and modal.

THE SELF-CONTAINMENT CONFLICT (found before any edit, resolved by inlining,
not by relaxing the existing guard). `agent/dashboard_server.py.
_serve_static`'s own docstring and `tests/test_dashboard_server.py::
test_command_center_html_makes_no_live_external_script_fetch` already commit
this codebase to a byte-identical, fully self-contained
`agent_command_center.html` with NO live external script/asset fetch at
page load -- React, ReactDOM and every font are already inlined as
compressed blobs for exactly this reason. CLAUDE_INTEGRATION.md's own
literal instructions (`<script src="./agent-core-zones.js">`, a sibling
`agent-core-original.png`) would reintroduce exactly the live fetch that
guard forbids. Resolved (with Ray's explicit sign-off) by inlining both
scripts verbatim as real top-level `<script>` tags in the same document,
and the PNG as a `data:image/png;base64,...` URI passed directly as the
custom element's own `asset` attribute -- no new sibling files, no new
manifest entries, `test_command_center_html_makes_no_live_external_script_
fetch` untouched and still passes.

WHY INLINE, NOT THE DC-RUNTIME'S OWN `<x-import>`. The compiled
`__bundler/template` blob is rendered through a bespoke template-to-React
compiler (`dc-runtime`, itself inlined as manifest entry
`e24bbd9e-dbbd-447f-8862-2dac5be49638`) that parses raw HTML via
`document.createElement("template").innerHTML = ...`. Any `<script>` placed
INSIDE that parsed template would be marked "already started" per the HTML
parsing spec and would never execute once reinserted via React's own
`createElement`/`appendChild` calls -- an unreliable, unverifiable path
with no headless browser available in this environment to confirm either
way (see this unit's own report). Plain `<script>` tags placed in the REAL,
outer, browser-parsed document (this test file's own subject) are
unambiguous: browser-parsed `<script>` elements always execute, no
polyfill/x-import resolution race involved. Both new scripts are placed
before the existing DOMContentLoaded-gated unpacking bootstrap has any
chance to run, so `customElements.define("agent-core-zones", ...)` and
`window.EnergyConnectors` are always ready before the dc-runtime ever
mounts the component tree that references them.

No test here makes a network call or needs a browser -- structural
assertions only, against the exact bytes `dashboard_server` serves, mirroring
`tests/test_dashboard_server.py`'s own established convention for this
file (raw substring/regex checks, e.g. `"support.js" not in html`), plus a
few assertions against the decoded `__bundler/template` JSON blob where a
structural check is cheaper and more precise than a raw substring match.
"""
from __future__ import annotations

import json
import re

from agent.dashboard_server import STATIC_DIR

HTML_BYTES = (STATIC_DIR / "agent_command_center.html").read_bytes()
HTML = HTML_BYTES.decode("utf-8")


def _decoded_template() -> str:
    m = re.search(r'<script type="__bundler/template">\n(.*?)\n\s*</script>', HTML, re.S)
    assert m, "no __bundler/template block found"
    return json.loads(m.group(1))


TEMPLATE = _decoded_template()


# ------------------------------------------------------- self-containment guard


def test_no_live_external_script_src_anywhere():
    """Same check as test_dashboard_server.py's own
    test_command_center_html_makes_no_live_external_script_fetch, repeated
    here as a standalone guard against this specific integration ever
    regressing back to CLAUDE_INTEGRATION.md's literal <script src=
    "./agent-core-zones.js"> instructions."""
    assert not re.search(r'<script[^>]+src=["\']https?://', HTML)
    assert 'src="./agent-core-zones.js"' not in HTML
    assert 'src="./energy-connectors.js"' not in HTML


def test_agent_core_zones_script_is_inlined_verbatim():
    assert 'customElements.define("agent-core-zones", AgentCoreZones)' in HTML
    assert "window.AgentCoreZones = AgentCoreZones" in HTML


def test_energy_connectors_script_is_inlined_verbatim():
    assert "class EnergyConnectors" in HTML
    assert "window.EnergyConnectors = EnergyConnectors" in HTML


def test_no_sibling_asset_files_were_added_to_static_dir():
    """The package's own README ships agent-core-original.png as a sibling
    file; this integration inlines it as a data: URI instead (see module
    docstring), so no new file should appear next to the served HTML."""
    names = {p.name for p in STATIC_DIR.iterdir()}
    assert "agent-core-zones.js" not in names
    assert "energy-connectors.js" not in names
    assert "agent-core-original.png" not in names


# --------------------------------------------------------------- core swap


def test_old_concentric_circle_core_illustration_is_gone():
    assert 'sc-camel-view-box="0 0 340 340"' not in TEMPLATE


def test_agent_core_zones_element_is_present_with_inlined_png_asset():
    assert '<agent-core-zones id="agentCore" size="340" asset="data:image/png;base64,' in TEMPLATE
    assert TEMPLATE.count("<agent-core-zones") == 1


# ---------------------------------------------------------- connector swap


CONNECTOR_IDS = [
    "path-collect", "path-screen", "path-budget", "path-analysis",
    "path-gate", "path-approval", "path-recon", "path-exec", "path-bus",
]


def test_every_connector_path_is_marked_data_energy_path():
    assert TEMPLATE.count("data-energy-path") == len(CONNECTOR_IDS)
    for path_id in CONNECTOR_IDS:
        assert f'id="{path_id}"' in TEMPLATE


def test_dash_animation_is_not_restored():
    """Non-negotiable per CLAUDE_INTEGRATION.md: never restore animated
    dashes or stroke-dashoffset."""
    assert "stroke-dasharray=\"6 12\"" not in TEMPLATE
    assert "@keyframes flow" not in TEMPLATE
    assert "stroke-dashoffset" not in TEMPLATE
    assert "animation: flow" not in TEMPLATE


def test_connector_layer_svg_has_a_stable_id():
    assert '<svg id="connectorLayer"' in TEMPLATE


# ------------------------------------------------------ mount/wiring logic


def test_component_constructs_energy_connectors_and_grabs_the_core_ref():
    assert 'document.getElementById("agentCore")' in TEMPLATE
    assert 'new EnergyConnectors(document.getElementById("connectorLayer"))' in TEMPLATE


def test_component_calls_set_processes_and_set_load():
    assert "this._agentCore.setProcesses(processes)" in TEMPLATE
    assert 'this._connectorFlow.setLoad("path-" + key, loads[key])' in TEMPLATE


def test_approval_drawer_state_does_not_enter_the_load_computation():
    """Non-negotiable per CLAUDE_INTEGRATION.md: human approval state does
    not determine core or connector color. `isOpen`/`D` (the drawer-open
    state) must not appear inside the `loads` object literal."""
    start = TEMPLATE.index("const loads = {")
    end = TEMPLATE.index("};", start)
    loads_block = TEMPLATE[start:end]
    assert "isOpen" not in loads_block
    assert re.search(r'\bD\b', loads_block) is None


def test_connector_flow_is_destroyed_on_unmount():
    assert "if (this._connectorFlow) this._connectorFlow.destroy();" in TEMPLATE


# ------------------------------------------------- preservation of everything else


def test_all_twelve_drawer_click_handlers_are_still_present_exactly_once():
    for name in ["openP1", "openP2", "openP3", "openP4", "openP5", "openP6",
                "openP7", "openP8", "openP9", "openP10", "openP11", "openCore"]:
        assert TEMPLATE.count(f"{name}:") == 1, f"{name} handler missing or duplicated"


def test_panel_titles_are_unchanged():
    for title in ["Data collection", "Materiality screen", "Risk gates",
                 "Improvement loop"]:
        assert title in TEMPLATE


def test_served_bytes_still_match_the_file_on_disk():
    """Guards against the served response ever diverging from the file this
    whole test module reads directly -- mirrors
    test_dashboard_server.py::test_root_serves_the_command_center_html_byte_identical."""
    assert HTML_BYTES == (STATIC_DIR / "agent_command_center.html").read_bytes()
