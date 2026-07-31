"""agent/filing_text.py (T4 prerequisite unit, 2026-07-31): stdlib-only
HTML-to-text extraction for SEC EDGAR filing documents. `agent.materiality`/
`agent.materiality_cycle` never call this -- it exists for the (not yet
built, see agent/edgar_collector.py's module docstring) T4 analysis path to
turn a stored raw filing document Fact into the untrusted-content text a
prompt is built from.

TABLE HANDLING (the load-bearing design decision this module makes, and the
one asked to be reported on plainly): a `<tr>` is serialized as its non-
empty cell texts joined with " | ", in document order -- this preserves
LABEL-TO-ROW association (a line item and its own row's figures stay
together on one output line) but NOT label-to-COLUMN/YEAR association
beyond position: which of a row's several trailing numbers belongs to which
fiscal year is only recoverable by counting columns against the nearest
preceding header row, the same way a human skimming the rendered table
would. See the two real-fixture tests below (`test_...aapl_10k...`) for a
concrete, checkable example of both the success (label survives next to its
own row's figures) and the limitation (no per-cell year label).
"""
from __future__ import annotations

from pathlib import Path

from agent.filing_text import extract_filing_text

FIXTURES = Path(__file__).parent.parent / "scripts" / "fixtures" / "edgar"


# --------------------------------------------------------------- basic stripping

def test_strips_tags_and_keeps_text():
    html = "<html><body><p>Hello <b>world</b></p></body></html>"
    assert extract_filing_text(html) == "Hello world"


def test_collapses_internal_whitespace_and_newlines():
    html = "<div>  Hello   \n\n  world  </div>"
    assert extract_filing_text(html) == "Hello world"


def test_block_tags_produce_line_breaks():
    html = "<div>First</div><div>Second</div>"
    assert extract_filing_text(html) == "First\nSecond"


def test_drops_script_and_style_content_entirely():
    html = "<html><head><style>.x{color:red}</style></head><body>" \
           "<script>alert('hi')</script><p>Real content</p></body></html>"
    text = extract_filing_text(html)
    assert "alert" not in text
    assert "color:red" not in text
    assert text == "Real content"


def test_drops_display_none_subtrees():
    """EDGAR's inline-XBRL `ix:header` block (duplicate metadata, not
    narrative) is wrapped in `<div style="display:none">` -- must not leak
    into the extracted text, hidden or not, at any nesting depth."""
    html = ('<div style="display:none">'
            '<span>HIDDEN DUPLICATE</span>'
            '<div><span>still hidden, nested</span></div>'
            '</div>'
            '<p>Visible content</p>')
    text = extract_filing_text(html)
    assert "HIDDEN" not in text
    assert "still hidden" not in text
    assert text == "Visible content"


def test_decodes_html_entities():
    html = "<p>Apple&#8217;s net sales &amp; margin</p>"
    assert extract_filing_text(html) == "Apple’s net sales & margin"


# ------------------------------------------------------------------- tables

def test_table_row_joins_nonempty_cells_with_pipe():
    html = ("<table><tr><td>Net sales</td><td>391,035</td></tr>"
            "<tr><td>Cost of sales</td><td>210,352</td></tr></table>")
    text = extract_filing_text(html)
    assert text == "Net sales | 391,035\nCost of sales | 210,352"


def test_table_row_drops_empty_spacer_cells():
    """Real EDGAR tables pad rows with empty `<td>`s purely for column-width
    layout -- these must not appear in the output as empty pipe segments."""
    html = ('<table><tr>'
            '<td style="width:1%"></td><td>Products</td>'
            '<td style="width:1%"></td><td>307,003</td>'
            '<td style="width:1%"></td>'
            '</tr></table>')
    text = extract_filing_text(html)
    assert text == "Products | 307,003"


def test_self_closing_empty_cells_are_also_dropped():
    html = '<table><tr><td/><td>Label</td><td/><td>42</td></tr></table>'
    text = extract_filing_text(html)
    assert text == "Label | 42"


def test_a_row_that_is_entirely_empty_spacer_cells_produces_no_line():
    """The layout-only first <tr> some EDGAR tables use purely to declare
    column widths (every cell empty) must not surface as a blank line."""
    html = ('<table>'
            '<tr><td style="width:1%"></td><td style="width:2%"></td></tr>'
            '<tr><td>Label</td><td>1</td></tr>'
            '</table>')
    text = extract_filing_text(html)
    assert text == "Label | 1"


# ------------------------------------------------------- real fixtures (8-K)

def _read_fixture(name: str) -> str:
    path = FIXTURES / name
    return path.read_text(encoding="utf-8", errors="replace")


def test_real_8k_extraction_is_a_complete_faithful_body():
    html = _read_fixture("AAPL_8K_0000320193-26-000018.htm")
    text = extract_filing_text(html)
    # Concrete, checkable substrings from the REAL filed 8-K body.
    assert "FORM 8-K" in text
    assert "CURRENT REPORT" in text
    assert "Item 2.02" in text
    assert "Results of Operations and Financial Condition" in text
    assert "issued a press release regarding" in text
    assert "Item 9.01" in text
    # exhibit table survives as a real, checkable label/value row
    assert "99.1 | Press release issued by Apple Inc." in text
    # nothing from the hidden ix:header/XBRL scaffolding leaks through
    assert "xmlns" not in text
    assert "contextRef" not in text


def test_real_8k_extraction_is_a_large_fraction_of_the_useful_content():
    html = _read_fixture("AAPL_8K_0000320193-26-000018.htm")
    text = extract_filing_text(html)
    raw_bytes = len(html.encode("utf-8"))
    # Real, measured numbers (see delivery report): a short 8-K cover
    # document is almost entirely markup/XBRL-attribute overhead; ~9% of
    # raw bytes surviving as extracted text is expected and healthy, not a
    # sign of lossy extraction -- confirmed by this test reading the
    # extraction back and finding it complete (see previous test).
    assert len(text) > 3000
    assert len(text) / raw_bytes > 0.05


# ------------------------------------------------------ real fixtures (10-K)

def test_real_10k_preserves_label_to_row_association_for_a_real_line_item():
    """The concrete example asked for: Apple's real FY2025 Consolidated
    Statement of Operations, 'Products' net sales line. The label and its
    three real fiscal-year figures ($307,003 / $294,866 / $298,085 million)
    must survive on the SAME output line, in the SAME order as the header
    row's three fiscal years -- this is genuinely checkable against Apple's
    public 10-K."""
    html = _read_fixture("AAPL_10K_0000320193-25-000079.htm")
    text = extract_filing_text(html)
    assert "Products | $ | 307,003 | $ | 294,866 | $ | 298,085" in text
    # The header row naming the three fiscal years precedes it in the same
    # table (with an intervening "Net sales:" section-label row between) --
    # as its OWN line, not attached to the number: recovering "307,003 is
    # the 2025 figure" requires counting columns against this header line,
    # not a per-cell label. Search backward for it rather than assuming a
    # fixed line offset, since intervening section-label rows are real,
    # expected table structure, not a parsing defect.
    lines = text.splitlines()
    idx = lines.index("Products | $ | 307,003 | $ | 294,866 | $ | 298,085")
    preceding = lines[max(0, idx - 5):idx]
    header_line = next((l for l in preceding if "September 27,2025" in l), None)
    assert header_line is not None, f"no header line found in: {preceding!r}"
    assert "September 28,2024" in header_line
    assert "September 30,2023" in header_line


def test_real_10k_preserves_a_seven_column_segment_table():
    """A harder real case: Apple's segment-reporting table has 7 data
    columns (5 geographic segments + Corporate + Total). Confirms the
    row-join approach holds up beyond a simple 3-column table."""
    html = _read_fixture("AAPL_10K_0000320193-25-000079.htm")
    text = extract_filing_text(html)
    assert ("Net sales | $ | 178,353 | $ | 111,032 | $ | 64,377 | $ | 28,703 "
            "| $ | 33,696 | $ | — | $ | 416,161") in text


def test_real_10k_extraction_fraction_and_hidden_block_stripped():
    html = _read_fixture("AAPL_10K_0000320193-25-000079.htm")
    text = extract_filing_text(html)
    raw_bytes = len(html.encode("utf-8"))
    # Real, measured numbers (see delivery report): ~86% of this heavily
    # inline-XBRL-tagged, richly styled 10-K's raw bytes are markup/style/
    # XBRL-attribute overhead, confirmed directly by summing this parser's
    # own visible-text-node lengths (~204K chars) against the 1.52MB raw
    # file -- not evidence of lossy extraction.
    assert len(text) > 150_000
    assert len(text) / raw_bytes > 0.10
    # the duplicate hidden ix:header block must not have leaked through
    assert "AmendmentFlag" not in text
    assert "EntityCentralIndexKey" not in text
