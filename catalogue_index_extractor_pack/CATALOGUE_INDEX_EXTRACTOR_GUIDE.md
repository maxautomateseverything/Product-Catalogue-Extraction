# Catalogue Index Extractor — Master Process and Logic Guide

## 1. Purpose

This tool extracts product-code/index information from **selectable-text PDF catalogue index pages**.

The minimum target output is always:

```text
Product Code / SKU
Catalogue Page Number
```

The tool is deliberately **semi-guided**, not fully autonomous. Catalogue index layouts vary too much for a fully automatic process to be trusted. The user provides catalogue-specific settings, and the script uses those settings to extract the index as safely as possible.

The key rule is:

```text
If a possible product code is found, do not silently discard it.
Extract it if possible. If uncertain, keep it and flag it for review.
```

---

## 2. Scope

### In scope

- One PDF catalogue at a time.
- Selectable/extractable PDF text.
- Manually defined index PDF page ranges.
- Index tables that flow vertically.
- Repeated table blocks on the same page, such as:

```text
Code | Pack/carton | Pallet | Page     Code | Pack/carton | Pallet | Page
```

- Required extraction of:
  - Product Code / SKU
  - Catalogue page reference
- Optional extraction of additional columns, such as:
  - Pack/carton
  - Pallet
  - Trade Price
  - Any other configured header
- Raw text audit to check that product-code candidates were not missed.
- Review files and issue diagnostics.
- Debug images for pages with issues only.

### Out of scope for this version

- Scanned/OCR-only catalogues.
- Automatic discovery of the index page range.
- Fully autonomous layout inference with no user configuration.
- Perfect extraction of every optional column without review.

---

## 3. Primary engine choice

The primary extraction engine is **pdfplumber word-coordinate extraction**.

Camelot is useful for table extraction, but this tool needs lower-level control than a normal table extractor provides. The core requirement is not simply:

```text
PDF table -> dataframe
```

The actual requirement is:

```text
PDF words with coordinates
-> configured header detection
-> detected column zones
-> ordered SKU/page pairing
-> raw text candidate audit
-> reviewable output
```

This is why the script uses pdfplumber as the primary engine.

---

## 4. High-level workflow

```text
1. Read config or prompt user interactively.
2. Open the selected PDF.
3. Process only the manually configured PDF index pages.
4. For each page:
   a. Extract words and coordinates.
   b. Detect required headers.
   c. Build table blocks from product-code/page header pairs.
   d. Build column zones from header x-coordinates.
   e. Extract product codes from the SKU column.
   f. Extract page references from the page column.
   g. Pair SKU 1 with page 1, SKU 2 with page 2, etc.
   h. Extract optional columns if configured and found.
   i. Run raw text SKU audit.
   j. Flag page/block/row issues.
5. Build one-row-per-SKU registry.
6. Add any raw-text-only SKU candidates to the registry as review items.
7. Validate user-supplied example SKU/page pairs.
8. Write CSV outputs.
9. Generate debug images for issue pages only.
```

---

## 5. User configuration

The tool supports two run modes:

### Mode A — Interactive setup

```powershell
py catalogue_index_extractor.py --interactive
```

The script prompts the user for all required settings and can save a reusable config file.

### Mode B — Saved config

```powershell
py catalogue_index_extractor.py --config catalogue_index_config.yaml
```

The script reads all settings from a YAML or JSON config file.

---

## 6. Required settings

The user must provide:

| Setting | Description | Example |
|---|---|---|
| Input PDF | The catalogue PDF to process | `catalogue.pdf` |
| Output folder | Folder where CSVs/debug files are written | `Index Output` |
| Index PDF pages | PDF page range to process (inclusive) | `1293-1364` |
| SKU header text | Exact text for product-code column | `Code` |
| Page header text | Exact text for catalogue-page column | `Page` |
| Expected table blocks | Expected repeated SKU/page blocks per page | `3` |
| Positive SKU examples | At least 3 example product codes | `GW 21 005`, `DX 56 225` |
| Page examples | Example page values | `985`, `12/13`, `A12` |
| SKU regex | User-approved regex for product-code detection | See config |

---

## 7. Optional settings

The user can also configure:

| Setting | Description |
|---|---|
| Optional columns | Additional headers to extract when found |
| Header case sensitivity | Case-sensitive or case-insensitive exact header matching |
| Known SKU/page pairs | User-provided validation examples |
| Manual coordinate blocks | User-defined x-ranges for difficult catalogues |
| Catalogue page offset | Optional reference offset for source index page display |
| Debug image generation | Enabled for issue pages only |

---

## 8. Example config

```yaml
input_pdf: "C:/Path/To/catalogue.pdf"
output_folder: "C:/Path/To/Index Output"
index_pdf_pages: "1293-1364"
page_source_of_truth: "pdf_page_number"

required_columns:
  sku:
    header_text: "Code"
  page:
    header_text: "Page"

optional_columns:
  - output_name: "pack_carton"
    header_text: "Pack/carton"
  - output_name: "pallet"
    header_text: "Pallet"

expected_table_blocks_per_page: 3

header_matching:
  case_sensitive: false

sku_detection:
  positive_examples:
    - "GW 21 005"
    - "GW D3 674"
    - "DX 56 225"
  negative_examples:
    - "GW 21"
  sku_regex: "\\b(?:GW|DX)(?:[\\s\\-_/\\.]+[A-Z0-9]{1,8}){2,5}\\b"

page_detection:
  positive_examples:
    - "985"
    - "12/13"
    - "A12"
  page_regex: "(?i)\\b(?:see\\s+page\\s+)?[A-Z]?\\d+(?:\\s*(?:,|/|;|-)\\s*[A-Z]?\\d+)*\\b"
  keep_original_and_normalized: true

example_validation_pairs:
  - sku: "GW 21 005"
    page: "1026"
  - sku: "DX 56 225"
    page: "358"

extraction_mode: "auto"

debug_images:
  enabled: true
  only_issue_pages: true

review_files:
  excel_safe: true
```

---

## 9. Page range logic

The user manually defines the index PDF page range.

Examples:

```text
1293-1364
1-3,7,10-12
```

The **PDF page number** is the source of truth because it is what the code can reliably address in the PDF file.

If a catalogue-page offset is supplied, the script preserves that as a reference column, but it does not use it to decide which pages to process.

---

## 10. Header detection logic

The script searches for exact header text matches using pdfplumber word coordinates.

Required headers:

```text
Product-code/SKU header
Catalogue-page header
```

For example:

```text
Code
Page
```

Optional headers are searched separately:

```text
Pack/carton
Pallet
```

### Case sensitivity

The user chooses whether header matching is case-sensitive.

Default recommendation:

```yaml
header_matching:
  case_sensitive: false
```

This means `Code`, `CODE`, and `code` can be treated as equivalent.

---

## 11. Table block detection logic

For every selected PDF page:

```text
1. Find all SKU header occurrences.
2. Find all page header occurrences.
3. Pair each SKU header with the nearest page header to its right on the same visual header line.
4. Each SKU/page header pair becomes one table block.
```

Example with three blocks:

```text
Code ... Page     Code ... Page     Code ... Page
```

If the user configured:

```yaml
expected_table_blocks_per_page: 3
```

but the script detects only 2 blocks on a page, the page is flagged in:

```text
page_diagnostics_review.csv
```

and a debug image is generated for that page.

---

## 12. Column zone logic

Within each detected table block, the script uses header x-coordinates to create column zones.

Example headers:

```text
Code | Pack/carton | Pallet | Page
```

The headers are sorted left-to-right. Column boundaries are created at the midpoints between neighbouring header centres.

This produces x-ranges such as:

```text
SKU column zone
Pack/carton column zone
Pallet column zone
Page column zone
```

Only text whose x-coordinate falls inside a column zone is used for that field.

---

## 13. Row pairing logic

The tool does **not** rely on a hard y-coordinate threshold to pair each SKU with each page value.

Instead, within each vertical table block:

```text
1. Extract all product-code values from the product-code column.
2. Extract all page values from the page column.
3. Sort both lists top-to-bottom.
4. Pair the first SKU with the first page value.
5. Pair the second SKU with the second page value.
6. Continue until the end of the block.
```

If the counts do not match:

```text
SKU count = 50
Page count = 49
```

then:

- the rows are still kept;
- the block is flagged;
- affected rows are marked `needs_review`;
- a debug image is generated for the issue page.

---

## 14. Product-code detection logic

The user must provide at least 3 positive product-code examples.

Example:

```text
GW 21 005
GW D3 674
DX 56 225
```

The script suggests a regex from these examples. The user can accept or edit it.

For Gewiss-style codes, a possible regex is:

```regex
\b(?:GW|DX)(?:[\s\-_/\.]+[A-Z0-9]{1,8}){2,5}\b
```

This supports codes with:

- spaces;
- hyphens;
- underscores;
- slashes;
- full stops;
- optional extra suffix groups.

Negative examples are optional. They are mainly useful for user thinking and regex review.

---

## 15. Page-value detection logic

Catalogue page values are treated as text, not only as numbers.

Supported examples include:

```text
456
A12
12/13
12, 13
12-13
See page 12
```

The output preserves both:

```text
Original page value
Normalized page value
```

Examples:

| Original | Normalized |
|---|---|
| `12, 13` | `12;13` |
| `12/13` | `12;13` |
| `See page 12` | `12` |
| `A12` | `A12` |

The script does not expand ranges such as `12-13`, because different catalogues may use that format differently. It keeps that value as an auditable page reference unless the user later chooses different normalization rules.

---

## 16. Optional column extraction

Optional columns are configured by exact header text.

Example:

```yaml
optional_columns:
  - output_name: "pack_carton"
    header_text: "Pack/carton"
  - output_name: "pallet"
    header_text: "Pallet"
```

Rules:

```text
If the optional header is found, extract it.
If the optional header is not found, do not fail the product row.
If optional column counts do not align with SKU counts, flag the optional field for review.
```

Optional column problems should never cause a product code to be dropped.

---

## 17. Raw text SKU audit

The script also extracts raw selectable text from every configured index page and applies the approved SKU regex to that text.

This creates:

```text
raw_text_sku_audit_review.csv
```

The audit checks:

```text
Every raw-text SKU candidate should appear in the final SKU registry.
```

If a product-code candidate appears in raw text but not in structured coordinate extraction, the script adds it to the registry with:

```text
source_method = raw_text_audit
confidence_status = needs_review
review_reason = raw_text_candidate_not_in_structured_output
```

This is the audit-safe rule that prevents possible product codes being silently lost.

---

## 18. Example pair validation

The user can provide known SKU/page pairs.

Example:

```yaml
example_validation_pairs:
  - sku: "GW 21 005"
    page: "1026"
```

The script checks that the final SKU registry contains that SKU and page value.

Output:

```text
example_validation_review.csv
```

Possible statuses:

| Status | Meaning |
|---|---|
| `found` | The known pair was found |
| `sku_not_found` | The SKU was not in the final registry |
| `page_not_found_for_sku` | The SKU exists, but the expected page was not found |

---

## 19. Manual coordinate mode

Automatic mode should be tried first.

Manual coordinate mode is available for catalogues where header detection is unreliable.

Run with:

```powershell
py catalogue_index_extractor.py --config catalogue_index_config.yaml --manual-coordinates
```

or choose manual mode during interactive setup.

The script prints example candidate coordinates from the first configured index page, such as:

```text
GW 20 923   x0=38.20  x1=88.50  top=103.40  bottom=111.70
1046        x0=216.50 x1=239.80 top=103.30  bottom=111.80
```

The user then enters manual x-ranges for:

```text
SKU column
Page column
Optional columns if required
```

Manual coordinates are saved into the config file.

---

## 20. Debug images

Debug images are generated **only for pages with issues**.

Issue examples:

- expected table blocks do not match detected blocks;
- SKU count does not match page count;
- raw text SKU candidates were not in structured output;
- optional column counts do not match SKU count;
- manual coordinate mode is being reviewed;
- extraction errors occurred.

Debug images are written to:

```text
debug_images/
```

They draw detected block and column-zone overlays so the user can see why the page was flagged.

---

## 21. Output files

### Main collected data file

```text
sku_registry.csv
```

This is the main output for product extraction.

It contains one row per unique SKU.

Core columns:

| Column | Description |
|---|---|
| `sku` | Product code as extracted |
| `sku_normalized` | Product code normalized for matching |
| `catalogue_pages_original` | Original extracted page references |
| `catalogue_pages_normalized` | Normalized page references |
| `source_pdf_pages` | Index PDF pages where the SKU was found |
| `source_catalogue_pages` | Optional catalogue-page reference for index pages |
| `source_table_blocks` | Detected table block numbers |
| `source_methods` | Extraction methods that found the SKU |
| `confidence_status` | `confirmed` or `needs_review` |
| `review_reason` | Reason review is needed |
| `occurrence_count` | Number of row occurrences contributing to the SKU |

Optional configured columns are added to this file.

This file is not Excel-formula-wrapped, because it is the actual collected data file.

---

### Review and audit files

```text
index_rows_review.csv
unresolved_rows_review.csv
page_diagnostics_review.csv
raw_text_sku_audit_review.csv
header_detection_review.csv
example_validation_review.csv
extractor_errors_review.csv
run_summary_review.csv
```

Review files are written with Excel safety enabled by default. This helps prevent product codes and page references from being converted into dates, numbers, or formulas when opened directly in Excel.

---

## 22. Meaning of confidence statuses

| Status | Meaning |
|---|---|
| `confirmed` | SKU/page extraction was structurally aligned and no issue was flagged for that row |
| `needs_review` | The SKU is kept, but there was an issue such as missing page, count mismatch, optional mismatch, or raw-text-only source |

The tool intentionally keeps uncertain rows.

---

## 23. What counts as a successful run?

A good run is not necessarily one with zero issues.

The main success conditions are:

```text
1. sku_registry.csv contains all expected SKUs.
2. raw_text_sku_audit_review.csv has no important unaccounted candidates.
3. known SKU/page validation pairs pass.
4. page_diagnostics_review.csv only contains explainable issues.
5. unresolved_rows_review.csv is small enough for manual review.
```

For product extraction, it is better to keep a few review candidates than to accidentally drop product codes.

---

## 24. Recommended operating process

### Step 1 — Run interactive setup

```powershell
py catalogue_index_extractor.py --interactive
```

Save the config when prompted.

### Step 2 — Review first output

Open:

```text
run_summary_review.csv
page_diagnostics_review.csv
example_validation_review.csv
```

### Step 3 — Check unresolved rows

Open:

```text
unresolved_rows_review.csv
```

Look for common causes:

- wrong SKU regex;
- missing page header detection;
- wrong expected table block count;
- optional column misalignment;
- manual x-ranges needed.

### Step 4 — Check debug images

Only issue pages get debug images.

Use them to decide whether to:

- adjust header text;
- change case sensitivity;
- edit the SKU regex;
- add manual coordinate blocks.

### Step 5 — Re-run from config

```powershell
py catalogue_index_extractor.py --config "C:\Path\To\catalogue_index_config.yaml"
```

### Step 6 — Use master SKU registry

Use:

```text
sku_registry.csv
```

as the driver for the next product extraction stage.

---

## 25. Important assumptions

| Area | Assumption |
|---|---|
| PDF type | Selectable text PDF |
| Index range | User defines it manually |
| Source of truth for processing | PDF page number |
| Required fields | SKU and catalogue page |
| Flow | Index rows flow vertically within each table block |
| Pairing | SKU values and page values are paired by order within a block |
| Optional columns | Extract only if headers are found |
| Safety | Never silently discard product-code candidates |
| Validation | Raw text audit must account for all SKU candidates |
| Output | UTF-8 CSV files |

---

## 26. Troubleshooting

### Problem: Expected 3 blocks, detected 2

Likely causes:

- header text does not exactly match;
- case sensitivity is wrong;
- one header is split differently in the PDF text;
- the page has a different layout.

Actions:

- check `header_detection_review.csv`;
- inspect the debug image;
- try case-insensitive matching;
- verify exact header text;
- use manual coordinate mode if needed.

---

### Problem: Many raw text SKUs added as `needs_review`

Likely causes:

- SKU regex is too broad;
- structured column zones are wrong;
- header detection missed table blocks;
- product codes appear outside the index table.

Actions:

- tighten the SKU regex;
- add negative examples to guide review;
- inspect `raw_text_sku_audit_review.csv`;
- review debug images.

---

### Problem: Known example pair failed

Likely causes:

- SKU regex missed the SKU;
- page regex normalized the page differently;
- wrong index page range;
- product appears in a different index section.

Actions:

- search `sku_registry.csv` for the SKU;
- check `catalogue_pages_original` and `catalogue_pages_normalized`;
- inspect the source index page.

---

### Problem: Optional columns are missing

Likely causes:

- optional header text does not exactly match;
- optional column is not on the same header row;
- the page does not contain that optional column.

Actions:

- check `header_detection_review.csv`;
- verify the exact optional header text;
- remember that optional column failures do not block SKU/page extraction.

---

## 27. File list in this pack

```text
catalogue_index_extractor.py
CATALOGUE_INDEX_EXTRACTOR_GUIDE.md
example_config_gewiss_style.yaml
requirements.txt
```

---

## 28. Summary

This process creates a reusable, configurable way to extract catalogue index data.

The strategy is:

```text
Use user-defined headers to locate table structure.
Use PDF coordinates to extract values from the correct columns.
Pair SKU/page values by vertical order within each table block.
Audit all raw selectable text for missed product-code candidates.
Keep uncertain candidates and flag them for review.
Output one master SKU registry for product extraction.
```
