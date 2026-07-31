"""HTML-to-text extraction for SEC EDGAR filing documents (T4 prerequisite
unit, 2026-07-31, inserted ahead of the T4 analysis layer's Commit 2 per
review: the analysis layer needs real filing NARRATIVE text to build a
prompt from, and this codebase had never fetched any -- only filing
METADATA, see agent/edgar_collector.py's module docstring).

stdlib only (`html.parser.HTMLParser`) -- no dependency added.

WHAT THIS DOES NOT DO: this module has no opinion on trust. The text it
returns is exactly as untrusted as the HTML it was given -- see
agent/edgar_collector.py's module docstring for where the untrusted-content
boundary is actually enforced (the T4 analysis layer's Commit 2, not yet
built). This module's only job is turning markup into readable text as
faithfully as stdlib parsing allows.

DESIGN DECISION -- TABLE HANDLING (asked to be reported on plainly, since
financial filings put the numbers that matter in tables): a `<tr>` becomes
one output line, built by joining that row's non-empty cell texts with
" | ", in document order. Empty cells (EDGAR's own layout-only spacer
`<td>`s, used pervasively for column-width control -- see the real 10-K
fixture) are dropped entirely rather than emitted as empty segments, and a
row that is ALL spacer cells produces no output line at all.

This preserves LABEL-TO-ROW association: a line item's own row keeps its
figures immediately adjacent to its label on one line (confirmed against a
real fixture in tests/test_filing_text.py -- Apple's FY2025 10-K
"Products | $ | 307,003 | $ | 294,866 | $ | 298,085" line, and a harder
7-column segment-reporting table). It does NOT preserve label-to-COLUMN
(i.e. label-to-FISCAL-YEAR) association beyond position: recovering which
of a row's several numbers is the 2025 figure vs. the 2024 figure requires
counting columns against the nearest preceding header row -- the same way
a human skimming the rendered table would, not an explicit per-cell
relabeling. AN ANALYSIS BUILT ON THIS TEXT CAN HONESTLY CLAIM "this row's
label and figures traveled together"; it CANNOT honestly claim "this
specific number is self-describing as the FY2025 figure" without also
being given (or itself re-deriving) the governing header row. Callers that
need an unambiguous per-cell fiscal-year binding should not rely on this
module alone.

HIDDEN CONTENT: SEC's inline-XBRL filings wrap a duplicate metadata block
(`ix:header`) in `<div style="display:none">` -- this and any
`<script>`/`<style>` subtree are excluded entirely, at any nesting depth,
so duplicate/non-narrative content never leaks into what an analysis reads
as if it were visible prose.
"""
from __future__ import annotations

import re
from html.parser import HTMLParser

_WHITESPACE_RUN = re.compile(r"\s+")

_BLOCK_TAGS = frozenset({
    "p", "div", "br", "tr", "table", "li", "h1", "h2", "h3", "h4", "h5", "h6",
    "section", "article", "header", "footer", "body", "html", "td", "th", "hr",
})
_SKIP_SUBTREE_TAGS = frozenset({"script", "style"})


def _is_hidden(attrs: list[tuple[str, str | None]]) -> bool:
    style = (dict(attrs).get("style") or "").replace(" ", "").lower()
    return "display:none" in style


class _FilingTextExtractor(HTMLParser):
    """Streaming (no DOM) HTML-to-text pass. See module docstring for the
    table-row-joining and hidden-subtree-exclusion design."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self._hidden_stack: list[bool] = []
        self._out: list[str] = []
        self._cell_buf: list[str] = []
        self._row_cells: list[str] = []
        self._row_depth = 0

    # -- tag handling -------------------------------------------------------
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        hidden = bool(self._hidden_stack and self._hidden_stack[-1]) or _is_hidden(attrs)
        self._hidden_stack.append(hidden)
        if tag in _SKIP_SUBTREE_TAGS:
            self._skip_depth += 1
            return
        if self._skip_depth or hidden:
            return
        if tag == "tr":
            self._row_depth += 1
            self._row_cells = []
        elif tag in ("td", "th"):
            self._cell_buf = []
        elif tag in _BLOCK_TAGS:
            self._out.append("\n")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        # Self-closing, e.g. `<td/>`, `<br/>` -- no matching handle_endtag
        # will fire, so a spacer <td/> must still count as an empty cell.
        hidden = bool(self._hidden_stack and self._hidden_stack[-1]) or _is_hidden(attrs)
        if self._skip_depth or hidden:
            return
        if tag in ("td", "th") and self._row_depth:
            self._row_cells.append("")
        elif tag in _BLOCK_TAGS:
            self._out.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if self._hidden_stack:
            self._hidden_stack.pop()
        if tag in _SKIP_SUBTREE_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if self._skip_depth:
            return
        if tag in ("td", "th"):
            text = "".join(self._cell_buf).strip()
            if self._row_depth:
                self._row_cells.append(text)
            self._cell_buf = []
        elif tag == "tr":
            self._row_depth = max(0, self._row_depth - 1)
            nonempty = [c for c in self._row_cells if c]
            if nonempty:
                self._out.append(" | ".join(nonempty) + "\n")
            self._row_cells = []
        elif tag == "table":
            self._out.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth or (self._hidden_stack and self._hidden_stack[-1]):
            return
        # Collapse ANY whitespace run in source text -- including a literal
        # newline inside a single text node, e.g. source-formatted
        # indentation between inline elements -- to one space. Only the
        # "\n" THIS parser itself injects (block-tag/table-row boundaries,
        # below) is allowed to become a real line break in the final
        # output; a stray newline in the filing's own markup formatting
        # must not fragment one sentence into two lines.
        normalized = _WHITESPACE_RUN.sub(" ", data)
        if self._row_depth:
            self._cell_buf.append(normalized)
        else:
            self._out.append(normalized)

    # -- output ---------------------------------------------------------
    def get_text(self) -> str:
        raw = "".join(self._out)
        lines = (line.strip() for line in raw.split("\n"))
        return "\n".join(line for line in lines if line)


def extract_filing_text(html: str) -> str:
    """Real filing HTML -> plain text. See module docstring for exactly
    what this preserves (label-to-row) and does not (label-to-column)."""
    parser = _FilingTextExtractor()
    parser.feed(html)
    parser.close()
    return parser.get_text()
