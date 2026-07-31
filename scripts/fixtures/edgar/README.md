# scripts/fixtures/edgar/

Real SEC EDGAR filing documents, fetched as raw bytes with a declaring
`User-Agent` (per sec.gov's own fair-access policy -- see `agent/edgar.py`'s
module docstring), committed so `tests/test_filing_text.py` and
`tests/test_edgar_collector.py` exercise the HTML-to-text extractor and the
document-fetch/store path against real EDGAR markup rather than a
synthetic approximation. Neither file is fetched by any test -- both are
read from disk.

| File | Company | Form | Accession number | CIK | Bytes | SHA-256 |
| --- | --- | --- | --- | --- | --- | --- |
| `AAPL_8K_0000320193-26-000018.htm` | Apple Inc. | 8-K | 0000320193-26-000018 | 320193 | 38,350 | `d8a173f0b8cb911e41d27ca69261bd1f0461940a73f7654fc46f41ec7912b660` |
| `AAPL_10K_0000320193-25-000079.htm` | Apple Inc. | 10-K | 0000320193-25-000079 | 320193 | 1,520,208 | `548ae59778cf08ee0f2ee088e7ece20d947076c3c01f74d2d65db4c2777e436a` |

Fetched 2026-07-31 (file mtimes confirm). Source path (EDGAR Archives,
confirmed directly against SEC's own "Accessing EDGAR Data" page,
sec.gov/search-filings/edgar-search-assistance/accessing-edgar-data,
fetched 2026-07-31 -- see `agent/edgar.py`'s module docstring for the exact
citation): `https://www.sec.gov/Archives/edgar/data/{cik, no leading
zeros}/{accession number, dashes removed}/{primary document filename}`.

## Why the 10-K matters for `edgar_document_max_bytes`

At 1,520,208 bytes, `AAPL_10K_0000320193-25-000079.htm` is a genuinely
routine primary 10-K document, not a hand-picked outlier -- confirming
`agent.config.Config.edgar_document_max_bytes`'s default (5,000,000 bytes)
is a real operational constraint, not a defensive formality. See that
field's own comment in `agent/config.py` for the full reasoning.

## What these are, concretely

Both are the FULL, real, inline-XBRL-tagged HTML `primaryDocument` SEC
serves for these two filings -- not the flattened/extracted text a web
reader would show, and not a reconstruction. Every `<ix:nonFraction>`,
`<div style="...">`, and layout-only spacer `<td>` present in SEC's own
response is present here verbatim. This matters for
`tests/test_filing_text.py`: it proves the extractor's table handling
against markup that actually uses SEC's real (frequently spacer-column-
heavy) table layout, not an idealized one.
