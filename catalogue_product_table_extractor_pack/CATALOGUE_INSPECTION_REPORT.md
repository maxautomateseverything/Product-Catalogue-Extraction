# Catalogue inspection report

## Purpose

This report identifies representative inspection points for the table-focused
catalogue extractor. It is not a claim that only these pages can fail. The
catalogues contain many repeated templates, so the selected pages are intended
to cover the principal structural patterns found during a complete native-text
scan of both PDFs.

## Files analysed

| Catalogue | PDF pages | Inferred printed-page offset |
|---|---:|---:|
| GEWISS Trade Catalogue 2025–26 | 1,368 | `PDF page = printed page + 1` |
| ROBUS UK & IRE 2026 | 604 | `PDF page = printed page + 2` |

The offsets were inferred by counting standalone printed page numbers near the
top and bottom edges of every PDF page. The modal offsets were strongly
dominant.

## GEWISS registry files

The uploaded GEWISS files contain:

| File | Rows | Role |
|---|---:|---|
| `sku_registry.csv` | 10,431 | Canonical unique-SKU registry |
| `index_rows.csv` | 12,303 | Occurrence-level index provenance |

Four SKUs are marked `needs_review`. They should remain part of the canonical
registry but must be flagged in review outputs.

The index rows point to printed catalogue pages. They do not prove that all
information for a SKU is confined to that page. The final extractor therefore
maps the printed page to a PDF page, creates a configurable nearby-page window,
and continues to accept exact SKU matches outside that window. Outside-window
matches are preserved and flagged, not rejected.

## Full-catalogue pattern scan

The scan used selectable PDF text and page geometry; it did not rely on OCR.

### GEWISS

The catalogue contains thousands of `Code` table headers, dense repeated SKU
tables, multi-table pages, matrices with several SKU cells per visual row,
colour/symbol columns, and a large SKU index near the end. The table pack must
distinguish product tables from the index pages while still using the index as
registry provenance.

### ROBUS

The catalogue contains approximately 487 `PRODUCT` labels and 253
`DIMENSIONS (mm)` blocks. Common page designs combine:

- A borderless dimensions table with SKUs down the left.
- A `PRODUCT` comparison matrix with SKUs across the top.
- Option-code tables whose values are not SKUs.
- Emergency, accessory and dimensions mini-tables on the same page.
- Icons and non-table product information surrounding the tables.

The table pack should extract only the tabular components. Features, benefits,
construction text and icon specifications belong in the separate non-table
extractor.

## Recommended inspection pages

The detailed list is supplied in `catalogue_inspection_points.csv`.

### Highest-priority GEWISS pages

| PDF page | Printed page | Pattern | Main risk |
|---:|---:|---|---|
| 33 | 32 | Image-adjacent table | Product image/caption incorrectly added as a table column; colour swatches |
| 49 | 48 | Dense product table | Column/header accuracy and high SKU density |
| 65 | 64 | Multi-code output matrix | Internal/right-hand SKU cells, merged values and one-to-many record generation |
| 66 | 65 | Narrative plus several tables | Table boundaries absorbing prose |
| 503 | 502 | Many table blocks | Stable table segmentation and checkpointing |
| 708 | 707 | Colour/material SKU lists | Attribute context outside conventional row headers |
| 1028 | 1027 | Symbol-oriented tables | Visual-only fields and dynamic headers |
| 1293 | 1292 | SKU index | Must be recognised as index provenance rather than normal product specifications |

### Highest-priority ROBUS pages

| PDF page | Printed page | Pattern | Main risk |
|---:|---:|---|---|
| 80 | 78 | Borderless dimensions | `Height/Depth` and `Width` merged into one extracted column |
| 149 | 147 | Several product blocks | Cross-association between independent products |
| 161 | 159 | Options plus product data | `E`, `STE`, `SEN` and similar option codes falsely treated as SKUs |
| 165 | 163 | SKUs across columns | Product matrix must be transposed into one occurrence per SKU |
| 178 | 176 | Main and emergency tables | Correct variant association |
| 254 | 252 | Modular variants | Several related matrices on one page |
| 448 | 446 | Six product labels | Segmentation stress test |
| 480 | 478 | Emergency product page | Multiple table types and option-code exclusions |
| 507 | 505 | Humanitas tables | Dimensions and product matrix beside icons and prose |
| 519 | 517 | Simple baseline | Control page for over-segmentation |

## Acceptance checks

For each inspection page, verify:

1. Every genuine table is detected and receives a stable table number.
2. Photographs, page headings, feature text and footers are excluded.
3. True merged cells are repeated only across the rows or columns they span.
4. Real blank cells remain blank.
5. SKU codes are recognised in left, right, internal and column-header positions.
6. Several SKUs in one row produce several product occurrences with shared
   attributes repeated.
7. Original headers and normalized headers are both retained.
8. Option codes and quantities are not misclassified as SKUs.
9. Exact registry matches outside the expected index window are retained and
   flagged.
10. Conflicting values are preserved as separate attribute occurrences and
    added to the review queue.
11. An interrupted run can resume without reprocessing completed tables.
12. AI is called only for routed exceptions unless explicitly forced.

## Suggested regression tiers

### Tier 1 — fast deterministic smoke test

Use one page for each basic orientation:

- GEWISS PDF 33
- GEWISS PDF 65
- ROBUS PDF 80
- ROBUS PDF 165

### Tier 2 — segmentation test

Add:

- GEWISS PDF 312
- GEWISS PDF 503
- ROBUS PDF 149
- ROBUS PDF 448

### Tier 3 — index and exception test

Add:

- GEWISS PDF 1293
- ROBUS PDF 161
- ROBUS PDF 480
- GEWISS PDF 708

The final production run should still process the full catalogue. These tiers
are for release testing and troubleshooting, not for limiting extraction.
