"""Schema-constrained parsing of the T4 model's raw output, citation
resolution, and the period-attribution check (§3.3, §10, T4 unit Commit 3).

REFUSAL, NOT PARTIAL ACCEPTANCE. `parse_analysis_output` validates the
ENTIRE payload atomically -- malformed JSON, a missing/wrong-typed field,
an empty bear case, an uncited claim, a citation that does not resolve to
a real fact as of the analysis instant, or (new, this round) a period-
specific claim whose cited excerpt lacks the header row establishing
column order -- every one of these raises `AnalysisRefused` for the WHOLE
analysis. There is no code path that returns a partially-valid
`AnalysisOutput` with the bad piece silently dropped; §10 requires a bear
case to be present, and an unsupported claim anywhere is exactly as
disqualifying as a citation to a fact that was never collected.

CITATION FORMAT: `<fact_id>` (matches a key in the `citation_index`
`agent.analysis_prompt.build_analysis_prompt` returned), or `<fact_id>
#L<n>` / `<fact_id>#L<n>-L<m>` for a `filing_document` fact, pointing at
specific 1-indexed line(s) of that fact's extracted text -- the same line
numbering the prompt itself showed the model (`agent.analysis_prompt`'s
own `L<n>:` rendering). A citation is valid only if BOTH: (1) its fact_id
is a key in `citation_index`, AND (2) a fact matching that same
entity_id/field/observed_at/source_id genuinely appears in the SUPPLIED
`AsOfView`'s own history as of `as_of` -- checked directly against the
store, not merely trusted from the in-memory index, so a citation_index
built for a later instant cannot be used to smuggle in a fact this
specific `as_of` could not yet have known (see
`test_citation_to_a_fact_not_yet_visible_as_of_the_analysis_instant_is_refused`).

PERIOD ATTRIBUTION (added this round). A table row surviving
label-adjacent-to-its-own-figures (see agent/filing_text.py's own
documented limitation) does not carry which FISCAL PERIOD each figure
belongs to -- that requires the governing header row, a few lines above.
A model citing such a row with confidence about which year a number
belongs to could be wrong, and a citation to the row alone would not catch
it. This module therefore:

1. DETECTS a "period-specific" claim via `_is_period_specific_claim`: the
   claim's TEXT contains BOTH an explicit period marker (a fiscal year,
   "FYyyyy", a quarter "Qn yyyy", or a bare "yyyy") AND a numeric figure.
   THIS IS A NAMED, LIMITED HEURISTIC OVER CLAIM TEXT, NOT TRUE
   UNDERSTANDING -- see WHAT THIS CANNOT DETECT below.

2. For every period-specific claim's `filing_document` citations (line-
   anchored), checks a bounded window of the `_HEADER_WINDOW` lines
   immediately preceding the cited line for something that
   `_looks_like_header_row` -- a line containing the phrase "year(s)
   ended" or at least two distinct 4-digit year tokens. If a period-
   specific claim has at least one filing_document citation and NONE of
   them have a qualifying header-row line in their window, the claim (and
   therefore the whole analysis) is refused.

   A period-specific claim citing ONLY a non-`filing_document` fact (e.g.
   `market_snapshot`, which describes a single instant, not a multi-column
   table) is NOT subject to this check -- the ambiguity this check exists
   for cannot occur there.

WHAT THIS CANNOT DETECT (stated plainly, not left as an implied guarantee):

- A period-specific claim that does NOT explicitly name a period in its
  own claim text (e.g. relying on conversational context, or phrased as
  "the most recent fiscal year" without naming it) is NOT flagged by
  `_is_period_specific_claim` at all -- it slips through with no header-row
  check applied, because the detector works over the claim's own words,
  not the model's actual reasoning.
- `_looks_like_header_row` is a pattern match, not a real understanding of
  table structure: it can false-negative on a header phrased in a way the
  regex does not match (abbreviated quarter labels, non-English filings, a
  header split across non-adjacent lines by unusual column layout), and it
  can false-positive on an unrelated line that happens to contain two
  4-digit numbers.
- Even when a qualifying header-row line IS found in the window, this
  module verifies only that the INFORMATION NEEDED to attribute the figure
  to the right period was available in the cited excerpt -- it does NOT
  verify that the model actually used it correctly (i.e. picked the right
  column). Confirming the model's own arithmetic/column alignment is a
  model-behaviour question outside what a citation check can enforce.

An unenforceable rule stated as enforced would be worse than this named
gap -- so this list is the honest boundary of what Commit 3 actually
checks, not aspirational.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

from .edgar_collector import FIELD_DOCUMENT
from .store import AsOfView

_HEADER_WINDOW = 8   # lines of preceding context checked for a header row

_PERIOD_MARKER_RE = re.compile(
    r"\b(?:FY\s?20\d\d|Q[1-4]\s?20\d\d|fiscal\s+(?:year\s+)?20\d\d|20\d\d)\b", re.I)
_FIGURE_RE = re.compile(r"\$?\d[\d,]*(?:\.\d+)?\s?(?:million|billion|thousand|%)?", re.I)
_HEADER_PHRASE_RE = re.compile(r"years?\s+ended", re.I)
_YEAR_TOKEN_RE = re.compile(r"20\d\d")

_CITATION_RE = re.compile(r"^([0-9a-f]{16})(?:#L(\d+)(?:-L(\d+))?)?$")


class AnalysisRefused(Exception):
    """The analysis as a whole is refused -- never a partial acceptance.
    The message names exactly which check failed."""


@dataclass(frozen=True)
class Claim:
    text: str
    citations: tuple[str, ...]


@dataclass(frozen=True)
class AnalysisOutput:
    bull_case: tuple[Claim, ...]
    bear_case: tuple[Claim, ...]
    contradicting_evidence: tuple[Claim, ...]
    confidence: float


def _is_period_specific_claim(text: str) -> bool:
    return bool(_PERIOD_MARKER_RE.search(text)) and bool(_FIGURE_RE.search(text))


def _looks_like_header_row(line: str) -> bool:
    if _HEADER_PHRASE_RE.search(line):
        return True
    return len(_YEAR_TOKEN_RE.findall(line)) >= 2


def _parse_citation_ref(raw: str) -> tuple[str, int | None]:
    m = _CITATION_RE.match(raw)
    if not m:
        raise AnalysisRefused(f"citation {raw!r} is not a recognised fact_id/line reference")
    fact_id, line_start, _line_end = m.group(1), m.group(2), m.group(3)
    return fact_id, (int(line_start) if line_start else None)


def _require(cond: bool, message: str) -> None:
    if not cond:
        raise AnalysisRefused(message)


def _parse_claim(raw: dict, *, section: str, index: int) -> Claim:
    _require(isinstance(raw, dict), f"{section}[{index}] must be an object")
    _require(isinstance(raw.get("text"), str) and raw["text"].strip(),
             f"{section}[{index}].text must be a non-empty string")
    citations = raw.get("citations")
    _require(isinstance(citations, list) and len(citations) > 0,
             f"{section}[{index}] has no citations -- every claim must cite a stored fact")
    _require(all(isinstance(c, str) for c in citations),
             f"{section}[{index}].citations must all be strings")
    return Claim(text=raw["text"], citations=tuple(citations))


def _fact_exists_as_of(fact_id_entry, view: AsOfView, citation_index: dict) -> None:
    cf = citation_index.get(fact_id_entry)
    if cf is None:
        raise AnalysisRefused(
            f"citation to fact_id {fact_id_entry!r} does not resolve -- nonexistent fact"
        )
    fact = cf.fact
    # Defense in depth: re-check directly against the SUPPLIED view's own
    # history, not merely the in-memory citation_index -- a citation_index
    # built for a later `as_of` must not let a not-yet-visible fact resolve
    # against an earlier one (see module docstring).
    history = view.history(fact.entity_id, fact.field)
    match = any(
        f.observed_at == fact.observed_at and f.source_id == fact.source_id
        for f in history
    )
    if not match:
        raise AnalysisRefused(
            f"citation to fact_id {fact_id_entry!r} does not exist as of the analysis "
            "instant -- nonexistent or future fact"
        )


def _validate_citation(raw_citation: str, *, citation_index: dict, view: AsOfView,
                       section: str, index: int) -> tuple[str, int | None]:
    fact_id, line_no = _parse_citation_ref(raw_citation)
    _fact_exists_as_of(fact_id, view, citation_index)
    cf = citation_index[fact_id]
    if line_no is not None:
        _require(cf.fact.field == FIELD_DOCUMENT,
                 f"{section}[{index}] cites a line number against a non-document fact_id")
        _require(cf.lines is not None and 1 <= line_no <= len(cf.lines),
                 f"{section}[{index}] cites line {line_no} which is out of range for "
                 f"fact_id {fact_id!r}")
    return fact_id, line_no


def _check_period_attribution(claim: Claim, *, citation_index: dict, section: str,
                              index: int) -> None:
    if not _is_period_specific_claim(claim.text):
        return
    doc_citations = []
    for raw in claim.citations:
        fact_id, line_no = _parse_citation_ref(raw)
        cf = citation_index.get(fact_id)
        if cf is not None and cf.fact.field == FIELD_DOCUMENT and line_no is not None:
            doc_citations.append((cf, line_no))
    if not doc_citations:
        return   # nothing to check against -- see module docstring
    for cf, line_no in doc_citations:
        window_start = max(0, line_no - 1 - _HEADER_WINDOW)
        window = cf.lines[window_start:line_no - 1]
        if any(_looks_like_header_row(line) for line in window):
            return   # at least one citation's window supports the claim
    raise AnalysisRefused(
        f"{section}[{index}] makes a period-specific claim ({claim.text!r}) but none of "
        "its filing_document citations have a header row establishing column order "
        f"within {_HEADER_WINDOW} preceding lines -- unsupported period attribution"
    )


def parse_analysis_output(raw_json: str, *, citation_index: dict, view: AsOfView,
                          as_of) -> AnalysisOutput:
    """Parse and fully validate one T4 model response. Raises
    `AnalysisRefused` on ANY schema, citation, or period-attribution
    failure -- see module docstring for the exact checks and their
    disclosed limits."""
    try:
        payload = json.loads(raw_json)
    except json.JSONDecodeError as exc:
        raise AnalysisRefused(f"response is not valid JSON: {exc}") from None

    _require(isinstance(payload, dict), "response must be a JSON object")
    for key in ("bull_case", "bear_case", "contradicting_evidence", "confidence"):
        _require(key in payload, f"response is missing required key {key!r}")

    confidence = payload["confidence"]
    _require(isinstance(confidence, (int, float)) and not isinstance(confidence, bool),
             "confidence must be a number")
    _require(0.0 <= float(confidence) <= 1.0, "confidence must be between 0.0 and 1.0")

    sections: dict[str, tuple[Claim, ...]] = {}
    for section in ("bull_case", "bear_case", "contradicting_evidence"):
        raw_list = payload[section]
        _require(isinstance(raw_list, list), f"{section} must be a list")
        if section == "bear_case":
            _require(len(raw_list) > 0,
                     "bear_case must be non-empty -- §10 requires one; an absent bear "
                     "case is not a valid analysis")
        claims = tuple(_parse_claim(raw, section=section, index=i)
                       for i, raw in enumerate(raw_list))
        for i, claim in enumerate(claims):
            for raw_citation in claim.citations:
                _validate_citation(raw_citation, citation_index=citation_index, view=view,
                                   section=section, index=i)
            _check_period_attribution(claim, citation_index=citation_index,
                                      section=section, index=i)
        sections[section] = claims

    return AnalysisOutput(
        bull_case=sections["bull_case"], bear_case=sections["bear_case"],
        contradicting_evidence=sections["contradicting_evidence"],
        confidence=float(confidence),
    )
