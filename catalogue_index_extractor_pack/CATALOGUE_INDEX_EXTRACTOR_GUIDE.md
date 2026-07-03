# Catalogue Index Extractor — Master Process and Logic Guide

## 1. Purpose

This tool extracts a reusable product-code index from selectable-text PDF catalogue index pages.

The minimum output is always:

```text
Product Code / SKU
Catalogue Page Number
```

The tool is deliberately semi-guided. The user configures the catalogue-specific settings, and the extractor uses those settings to extract data safely. The core rule is:

```text
If a possible product code is found, keep it or flag it. Do not silently discard it.
```

---

## 2. What changed in this version

This version fixes the main issues seen in the previous runs:

| Previous problem | Change made |
|---|---|
| Optional values could shift rows | Extraction is now row-first, not column-list pairing. |
| Title rows like `DX 26` leaked into optional columns | Ignore-row patterns are applied before extracting required or optional values. |
| `Pack/carton` values such as `AB 10/200` occurred | Optional columns can now have value regex validators and raw/clean/status outputs. |
| Raw audit scanned too broadly | Raw SKU audit now scans only the detected SKU/Product Code column zones. |
| Footer page numbers caused page mismatches | The extractor uses row-first SKU+page logic and requires a manual `data_bottom` table cutoff. |
| Debug image generation failed on float coordinates | Debug overlay coordinates are rounded before drawing. |
| Debug images were hard to interpret | Issue-page images now label detected zones, e.g. `B1 sku`, `B1 pack_carton`. |
| Output had too many review files | Review/inspection outputs are now consolidated into one workbook. |

---

## 3. Scope

### In scope

- One PDF at a time.
- Selectable/extractable PDF text.
- User-defined PDF index page range.
- Vertically flowing index tables.
- Multiple repeated table blocks on the same page.
- Required extraction of SKU/Product Code and catalogue page.
- Optional extraction of configured columns.
- Manual table bottom cutoff.
- Automatic or manual x-coordinate column boundaries.
- Row-level validation groups.
- Review workbook and issue-page debug images.

### Out of scope

- Scanned/OCR-only catalogues.
- Automatic detection of the index range.
- Fully autonomous extraction without user configuration.

---

## 4. Core extraction model

The extractor now uses **row-first extraction**.

Previous logic:

```text
Extract every SKU in a column.
Extract every page in a column.
Extract every optional value in a column.
Pair item 1 with item 1, item 2 with item 2, etc.
```

New logic:

```text
For each detected table block:
  group PDF words into visual rows
  for each visual row:
    read the SKU column on that row
    read the page column on that row
    read optional columns on that row
    keep the row only if a SKU is found
    confirm it only if SKU and page are both found
```

This prevents rows such as `DX 26` from shifting every optional value underneath it.

---

## 5. Primary engine

The primary engine is **pdfplumber word-coordinate extraction**.

The tool does not rely on Camelot tables because this use case needs precise control over:

```text
headers
x-coordinates
column zones
visual rows
raw SKU-column audit
issue images
manual configuration
```

---

## 6. Required configuration

A run requires:

| Config field | Meaning |
|---|---|
| `input_pdf` | PDF catalogue file to process. |
| `output_folder` | Folder where outputs are written. |
| `index_pdf_pages` | PDF page numbers containing the index. |
| `required_columns.sku.header_text` | Exact header text for product codes. |
| `required_columns.page.header_text` | Exact header text for catalogue pages. |
| `expected_table_blocks_per_page` | Expected number of repeated SKU/page blocks per page. |
| `sku_detection.positive_examples` | At least three valid product-code examples. |
| `sku_detection.sku_regex` | Approved product-code regex. |
| `advanced.data_bottom` | Required y-coordinate cutoff for the bottom of the index table. |

The PDF page number is the source of truth for selecting pages.

---

## 7. Example config for Gewiss-style index pages

```yaml
input_pdf: "C:/Path/To/gewiss-trade-catalogue.pdf"
output_folder: "C:/Path/To/Index Output"
index_pdf_pages: "1293-1364"
page_source_of_truth: "pdf_page_number"

required_columns:
  sku:
    header_text: "Code"
    alignment: "left"
    inclusion_mode: "left"
  page:
    header_text: "Page"
    alignment: "left"
    inclusion_mode: "left"

optional_columns:
  - output_name: "pack_carton"
    header_text: "Pack/carton"
    alignment: "left"
    inclusion_mode: "left"
    value_regex: "^[0-9]+(?:/[0-9]+)*$"
    invalid_value_action: "blank_and_warn"
  - output_name: "pallet"
    header_text: "Pallet"
    alignment: "left"
    inclusion_mode: "left"
    value_regex: "^[0-9]+$"
    invalid_value_action: "blank_and_warn"

expected_table_blocks_per_page: 3

header_matching:
  case_sensitive: false

sku_detection:
  positive_examples:
    - "GW 21 005"
    - "GW D3 674"
    - "DX 56 225"
    - "DX 15 825 R"
    - "GW 10 051 AB"
    - "GW 10 159 F"
  negative_examples:
    - "DX 26"
    - "GW 21"
  sku_regex: "\\b(?:GW|DX)(?:[\\s\\-_/\\.]+[A-Z0-9]{1,8}){2,5}\\b"

sku_rules:
  uppercase_only: true
  allowed_characters: "A-Z0-9 space hyphen slash dot underscore plus"

page_detection:
  positive_examples:
    - "344"
    - "839"
    - "12/13"
  page_regex: "(?i)\\b(?:see\\s+page\\s+)?[A-Z]?\\d+(?:\\s*(?:,|/|;|-)\\s*[A-Z]?\\d+)*\\b"
  keep_original_and_normalized: true

ignore_row_patterns:
  - "^DX\\s+\\d+$"
  - "^GW\\s+\\d+$"

example_validation_groups:
  - code: "DX 10 016 R"
    pack_carton: "100/6400"
    pallet: "6400"
    page: "344"
  - code: "GW 15 415"
    pack_carton: "1/12"
    page: "839"

extraction_mode: "auto"

debug_images:
  enabled: true
  only_issue_pages: true
  label_zones: true

review_files:
  excel_safe: true
  write_separate_csvs: false

advanced:
  header_y_tolerance: 6.0
  line_y_tolerance: 3.0
  data_start_padding: 1.0
  x_tolerance: 1
  y_tolerance: 3
  data_bottom: 760
  boundary_overlap_warning_threshold: 0.75
```

---

## 8. Header detection

For every configured PDF page, the extractor searches for exact header text using pdfplumber word coordinates.

Required headers:

```text
SKU/Product Code header
Catalogue Page header
```

Optional headers are searched separately.

Header matching can be case-sensitive or case-insensitive:

```yaml
header_matching:
  case_sensitive: false
```

---

## 9. Table block detection

The extractor pairs each SKU header with the nearest page header to its right on the same visual header line.

Example:

```text
Code ... Page     Code ... Page     Code ... Page
```

If `expected_table_blocks_per_page` is `3`, but the extractor finds `2`, the page is marked for review and a debug image is created.

---

## 10. Column boundary logic

The extractor uses header x-coordinates and the configured column alignments to build x-ranges.

The goal is to avoid text from one column leaking into another.

### Supported alignments

| Alignment | Meaning |
|---|---|
| `left` | Values are expected to begin near the left side of the column. |
| `right` | Values are expected to end near the right side of the column. |
| `center` | Values are centred; automatic boundaries are unsafe, so manual coordinates are requested. |

### Automatic boundary rules

Assume Column 1 is left of Column 2.

#### Column 1 left, Column 2 left

```text
Column 1: from Column 1 header x0 to Column 2 header x0
Column 2: starts at Column 2 header x0
```

Example:

```text
Code starts at x=20
Pack/carton starts at x=105

Code zone = 20 to 105
Pack/carton zone starts at 105
```

#### Column 1 left, Column 2 right

```text
Column 1: from Column 1 header x0 to Column 2 header x0
Column 2: starts at Column 2 header x0
```

#### Column 1 right, Column 2 left

```text
Column 1: from Column 1 header x0 to Column 2 header x0
Column 2: starts at Column 2 header x0
```

#### Column 1 right, Column 2 right

```text
Column 1: from Column 1 header x0 to Column 1 header x1
Column 2: starts at Column 1 header x1
```

#### Any centre-aligned column

Automatic boundaries are not used. The script prompts for manual coordinate input, even when running from a saved config.

The terminal prints example data-row coordinates to help the user define the x-ranges.

---

## 11. Word inclusion and boundary review

The extractor never cuts text halfway. It only includes or excludes whole pdfplumber words.

If a word crosses a column boundary, the word is still included. It is flagged only when the overlap is significant enough to be suspicious. The default warning threshold is:

```yaml
advanced:
  boundary_overlap_warning_threshold: 0.75
```

This means a word is flagged when less than 75% of its width sits inside the column zone. This avoids noisy warnings where a value is visually correct but its PDF coordinates drift slightly over the header-derived boundary.

---

## 12. SKU parsing and cleanup

The SKU parser keeps:

| Column | Meaning |
|---|---|
| `sku_raw` | Raw text seen in the SKU column. |
| `sku` | Clean extracted SKU. |
| `sku_normalized` | SKU stripped of punctuation/spaces and uppercased for matching. |

When `uppercase_only: true`, lowercase letters are removed from the cleaned SKU candidate but preserved in `sku_raw`.

This handles small symbol-like characters that appear as lowercase letters in extracted text.

Unusual symbols are removed from the clean SKU according to `allowed_characters`.

---

## 13. Preventing neighbouring-column leakage into SKU

The SKU extractor can stop at values that match other configured column patterns.

Example raw line:

```text
DX 10 020 R 100/5200
```

With `pack_carton.value_regex` set to:

```regex
^[0-9]+(?:/[0-9]+)*$
```

The token `100/5200` is recognised as a Pack/carton value, so the SKU is kept as:

```text
DX 10 020 R
```

---

## 14. Page value extraction

The page column is treated as text.

Supported examples:

```text
344
839
A12
12/13
12, 13
12-13
See page 12
```

Outputs preserve both:

| Output | Meaning |
|---|---|
| `catalogue_page_original` | Raw extracted page value. |
| `catalogue_page_normalized` | Normalized value for matching. |

Examples:

| Original | Normalized |
|---|---|
| `12, 13` | `12;13` |
| `12/13` | `12;13` |
| `See page 12` | `12` |
| `A12` | `A12` |

---

## 15. Required bottom cutoff

The user must define:

```yaml
advanced:
  data_bottom: 760
  boundary_overlap_warning_threshold: 0.75
```

This is the bottom y-coordinate of the index table. It prevents footer page numbers or other bottom-of-page text from being treated as index data.

The cutoff is global for all index pages in this version.

---

## 16. Optional column validation

Optional columns can define a regex. If no regex is supplied, any text in that optional column is accepted.

For Gewiss-style quantity columns:

```yaml
pack_carton:
  value_regex: "^[0-9]+(?:/[0-9]+)*$"

pallet:
  value_regex: "^[0-9]+$"
```

This accepts:

```text
10/200
100/6400
60/4620
18000
6400
```

and rejects:

```text
AB 10/200
DX
GW
DX 10
```

Rejected optional values do not make the product row fail. They are warnings, not required issues.

Each optional column has:

| Column | Meaning |
|---|---|
| `<name>_raw` | Raw text extracted from the optional column. |
| `<name>` | Clean validated value. |
| `<name>_status` | `confirmed`, `missing`, `invalid_blank`, or `invalid_kept`. |

---

## 17. Title/section rows

Title rows can appear inside table blocks, for example:

```text
DX 26
GW 21
```

These are not product rows.

Use `ignore_row_patterns` to remove them before required and optional column extraction:

```yaml
ignore_row_patterns:
  - "^DX\\s+\\d+$"
  - "^GW\\s+\\d+$"
```

This prevents title-row text from becoming Pack/carton or Pallet values.

---

## 18. Raw SKU-column audit

The raw text audit now scans only detected SKU/Product Code column zones.

It does **not** scan:

- descriptions;
- notes;
- image captions;
- optional columns;
- full-page raw text.

The audit checks whether a SKU-like candidate found in the SKU column is present in structured SKU+page rows.

If not, it is added to the registry as:

```text
source_method = sku_column_raw_audit
confidence_status = needs_review
required_issues = raw_sku_column_candidate_not_in_structured_rows
```

---

## 19. Validation groups

Validation groups check that configured values appear in the **same extracted row**.

Example:

```yaml
example_validation_groups:
  - code: "DX 10 016 R"
    pack_carton: "100/6400"
    pallet: "6400"
    page: "344"

  - code: "GW 15 415"
    pack_carton: "1/12"
    page: "839"
```

Rules:

- `code` is required.
- `page` is recommended.
- Optional fields are checked only when supplied.
- A missing optional field in the validation group is not a failure.
- If the group fails, it appears in the workbook and the related page is treated as needing review when identifiable.

This is specifically designed to catch row-shift problems where, for example, a section title is accidentally read as `pack_carton`.

---

## 20. Output files

The output set is intentionally reduced.

### Main data files

```text
sku_registry.csv
index_rows.csv
```

These are UTF-8 CSV files.

`sku_registry.csv` is one row per unique SKU. Use it as the driver for the next product extraction stage.

`index_rows.csv` is one row per extracted SKU/page occurrence.

### Review workbook

```text
extraction_review_workbook.xlsx
```

All review and inspection data is consolidated into this workbook.

### Debug images

```text
debug_images/
```

Only pages with required issues or raw audit issues get debug images.

### Config used

```text
catalogue_index_config_used.yaml
```

This preserves the exact config used for the run.

---

## 21. Review workbook sheets

### Run Summary

High-level run counts:

| Metric | Meaning |
|---|---|
| `sku_registry_rows` | Number of unique SKUs in the registry. |
| `index_rows` | Row-level extraction count. |
| `unresolved_rows` | Rows requiring review. |
| `pages_processed` | Number of PDF pages processed. |
| `pages_needing_review` | Pages with required/raw audit issues. |
| `sku_column_raw_audit_rows` | Raw SKU-column audit candidate rows. |
| `raw_audit_candidates_added` | Raw audit candidates added to registry. |
| `extractor_errors` | Unexpected extraction errors. |

### Page Diagnostics

Page-level health check.

Important columns:

| Column | Meaning |
|---|---|
| `status` | `ok` or `needs_review`. |
| `expected_table_blocks` | User-configured expected block count. |
| `detected_table_blocks` | Detected block count. |
| `structured_rows` | Extracted row count on the page. |
| `structured_confirmed_rows` | Rows with SKU and page extracted cleanly. |
| `sku_column_raw_audit_rows` | SKU candidates found in SKU columns. |
| `sku_column_raw_audit_unaccounted` | SKU-column candidates not in structured rows. |
| `required_issues` | Serious SKU/page/block problems. |
| `optional_warnings` | Non-blocking optional column problems. |
| `raw_audit_issues` | Raw SKU-column audit issues. |
| `debug_image_path` | Debug image for issue pages. |

Issue cells contain actual line breaks for readability.

### Index Rows Review

Row-level detail for every extracted occurrence.

Use this sheet when a SKU registry row needs tracing back to the source page and row.

### Unresolved Rows

Rows that require manual review.

Common causes:

- SKU found but page missing.
- Boundary overlap in required columns.
- Raw SKU-column candidate not in structured rows.
- Optional value invalid or blanked.

### Raw SKU Column Audit

Shows SKU-like candidates found only in the SKU/Product Code column zones.

Use this to verify the extractor has not missed product codes.

### Header Detection

Shows detected header coordinates.

Useful when:

- expected table blocks do not match detected blocks;
- column boundaries look wrong;
- manual coordinate mode may be required.

### Validation Groups

Shows whether configured row validation groups were found.

### Extractor Errors

Unexpected runtime/page-level extraction errors.

---

## 22. Debug images

Debug images are generated only for issue pages.

They show:

- table block boundary in red;
- column zones in blue;
- labels such as `B1 sku`, `B1 pack_carton`, `B1 pallet`, `B1 page`.

Use them to verify:

- the SKU column starts and ends correctly;
- the optional columns do not overlap SKU values;
- the page column excludes footer text due to `data_bottom`;
- detected blocks match the visual page.

---

## 23. How to review a run

1. Open `extraction_review_workbook.xlsx`.
2. Check `Run Summary`.
3. Open `Page Diagnostics` and filter `status = needs_review`.
4. For each issue page, open the linked debug image.
5. If block count is wrong, check `Header Detection`.
6. If raw SKU audit found unaccounted candidates, check `Raw SKU Column Audit`.
7. If optional values look wrong, check `_raw`, clean value, and `_status` fields in `Index Rows Review`.
8. Check `Validation Groups`; all critical examples should be `found`.
9. Re-run after editing config.
10. Use `sku_registry.csv` once review is acceptable.

---

## 24. Troubleshooting

### Problem: `AB 10/200` appears in Pack/carton

Likely cause:

- SKU suffix leaked into the Pack/carton zone.
- Column boundary is too far left/right.

Fixes:

- Confirm `alignment: left` for all Gewiss columns.
- Check the debug image.
- Use manual coordinates if needed.
- Keep `pack_carton.value_regex` enabled so invalid values are blanked and warned.

### Problem: `DX 26` becomes Pack/carton/Pallet

Likely cause:

- Section title row was extracted as optional data.

Fix:

```yaml
ignore_row_patterns:
  - "^DX\\s+\\d+$"
  - "^GW\\s+\\d+$"
```

### Problem: Printed page footer appears in page column

Fix:

- Set `advanced.data_bottom` to the bottom of the table, above the footer.

### Problem: Centre-aligned columns

Automatic boundaries are unsafe. The script prompts for manual x-ranges.

### Problem: Debug image says float cannot be interpreted as integer

Fixed in this version. Overlay coordinates are rounded before drawing.

---

## 25. Recommended Gewiss settings

For the Gewiss catalogue index, start with:

```yaml
required_columns:
  sku:
    header_text: "Code"
    alignment: "left"
    inclusion_mode: "left"
  page:
    header_text: "Page"
    alignment: "left"
    inclusion_mode: "left"

optional_columns:
  - output_name: "pack_carton"
    header_text: "Pack/carton"
    alignment: "left"
    inclusion_mode: "left"
    value_regex: "^[0-9]+(?:/[0-9]+)*$"
    invalid_value_action: "blank_and_warn"
  - output_name: "pallet"
    header_text: "Pallet"
    alignment: "left"
    inclusion_mode: "left"
    value_regex: "^[0-9]+$"
    invalid_value_action: "blank_and_warn"

sku_rules:
  uppercase_only: true
  allowed_characters: "A-Z0-9 space hyphen slash dot underscore plus"

ignore_row_patterns:
  - "^DX\\s+\\d+$"
  - "^GW\\s+\\d+$"
```

---

## 26. Summary

The strategy is now:

```text
Use exact headers to locate table blocks.
Use configured alignment to build column boundaries.
Use row-first extraction so optional data cannot shift rows.
Keep raw and clean values for auditability.
Validate optional columns without making them required issues.
Audit only the SKU column for missed product-code candidates.
Use validation groups to confirm full row correctness.
Generate issue-page debug images with labelled zones.
Output two CSVs plus one review workbook.
```
