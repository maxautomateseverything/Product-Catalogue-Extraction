# Catalogue Index Extractor — Complete User and Logic Guide

## 1. Purpose

This tool extracts a reusable product-code index from selectable-text PDF catalogues.

The minimum output is always:

```text
Product Code / SKU
Catalogue Page Number
```

The purpose is to build a trusted SKU/page registry that can then drive later product-table extraction. The tool is deliberately **semi-guided**. Catalogue index layouts vary, so the user supplies catalogue-specific settings and the script produces both clean outputs and review evidence.

The central safety rule is:

```text
Do not silently lose product codes.
Confirmed rows are used directly.
Uncertain rows are retained and flagged for review.
```

---

## 2. Files in the pack

```text
catalogue_index_extractor.py          # Main extraction engine
catalogue_region_selector.py          # Visual PDF column selector / template builder
CATALOGUE_INDEX_EXTRACTOR_GUIDE.md    # This guide
example_config_gewiss_style.yaml      # Example config for Gewiss-style index pages
requirements.txt                      # Python dependencies
```

---

## 3. Install requirements

From the folder containing the scripts:

```powershell
py -m pip install -r requirements.txt
```

Dependencies:

| Package | Purpose |
|---|---|
| `pdfplumber` | Primary text/coordinate extraction engine |
| `PyYAML` | YAML config loading/saving |
| `Pillow` | Debug image support and GUI image display |
| `openpyxl` | Review workbook output |
| `PyMuPDF` | Visual selector PDF rendering |

---

## 4. Extraction modes

The extractor now supports three modes.

| Mode | Config value | Purpose |
|---|---|---|
| Automatic header mode | `auto` | Finds configured headers and builds column zones automatically |
| Terminal manual coordinates | `manual` | User types x/y boundaries in the terminal |
| Visual template mode | `visual_template` | User draws column boxes on a PDF preview and applies those coordinates |

The existing automatic and manual modes are not deprecated. Visual template mode is an additional way to create safer manual coordinates.

---

## 5. Recommended process

For a new catalogue, use this sequence:

```text
1. Create/edit a config file.
2. Scan header layouts across the index pages.
3. If one layout exists, draw one visual template.
4. If multiple layouts exist, draw one visual template per layout/page group.
5. Run extraction using visual_template mode or auto mode.
6. Review sku_registry.csv and extraction_review_workbook.xlsx.
7. Adjust config/template and rerun if needed.
```

---

## 6. Main run commands

### 6.1 Interactive setup

```powershell
py catalogue_index_extractor.py --interactive
```

This prompts for the required config and optionally saves a reusable config file.

### 6.2 Run from config

```powershell
py catalogue_index_extractor.py --config "C:\Path\To\catalogue_index_config.yaml"
```

### 6.3 Override input/output/pages from config

```powershell
py catalogue_index_extractor.py --config "C:\Path\To\catalogue_index_config.yaml" --input "C:\Path\To\catalogue.pdf" --output "C:\Path\To\Output" --pages "1293-1364"
```

### 6.4 Terminal manual coordinate mode

```powershell
py catalogue_index_extractor.py --config "C:\Path\To\catalogue_index_config.yaml" --manual-coordinates
```

### 6.5 Scan unique header layouts

```powershell
py catalogue_index_extractor.py --config "C:\Path\To\catalogue_index_config.yaml" --scan-layouts
```

This writes:

```text
layout_scan_review.csv
layout_scan_summary.csv
layout_scan_summary.json
```

Use this before visual selection. If the scan shows multiple unique layouts, create one visual template per layout group.

### 6.6 Run with one visual template

```powershell
py catalogue_index_extractor.py --config "C:\Path\To\catalogue_index_config.yaml" --visual-template "C:\Path\To\layout_1_template.json" --visual-template-pages "1293-1364"
```

The script attaches the template to the run config, switches to `visual_template` mode, and applies the drawn coordinates to the chosen pages.

---

## 7. Visual template workflow

### 7.1 Open the visual selector

```powershell
py catalogue_region_selector.py --pdf "C:\Path\To\catalogue.pdf" --config "C:\Path\To\catalogue_index_config.yaml" --page 1312 --zoom 2.0 --output-template "C:\Path\To\layout_1_template.json" --output-config "C:\Path\To\catalogue_index_config_with_visual.yaml" --apply-pages "1293-1364"
```

### 7.2 What to draw

For each repeated table block, draw one rectangle per column:

```text
block_1 sku
block_1 pack_carton
block_1 pallet
block_1 page

block_2 sku
block_2 pack_carton
block_2 pallet
block_2 page

block_3 sku
block_3 pack_carton
block_3 pallet
block_3 page
```

Draw each rectangle so it includes:

```text
The column header
The data below the header
The bottom cutoff where table data should stop
```

The extractor validates that the required headers are inside the selected regions. If it finds the headers, it moves the data start point below the headers so duplicate header text is not extracted as a data row.

### 7.3 GUI controls

| Control | Purpose |
|---|---|
| Prev Page / Next Page | Move between pages |
| Page + Go | Jump to a PDF page |
| Zoom + Apply Zoom | Re-render at a fixed zoom |
| Drag on canvas | Select a rectangular region |
| Region dialog | Assign block number, field name, alignment, inclusion mode |
| Delete Last | Remove the last selected region |
| Clear | Remove all selected regions |
| Save Template | Save JSON template |
| Save + Close | Save JSON and close |

### 7.4 Why the preview image is safe

The visual selector renders the PDF page only so the user can draw boxes. Extraction still uses the original PDF text layer.

Correct:

```text
Original PDF → rendered image preview → user boxes → original PDF text extraction
```

Incorrect:

```text
Original PDF → image → new image-only PDF → text extraction
```

---

## 8. Layout scanning logic

The layout scanner processes every configured index PDF page and records where the configured headers appear.

It uses the current config:

```yaml
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
```

For each page, the scanner attempts to build automatic column zones. It then creates a layout signature based on rounded coordinates.

If the same headers appear at the same coordinates across many pages, those pages belong to one layout group. If the headers appear at different coordinates, those pages become a different layout group.

### How to use the scan result

Open `layout_scan_summary.csv`.

| Column | Meaning |
|---|---|
| `layout_id` | Unique layout group |
| `representative_pdf_page` | Page to use for drawing a visual template |
| `pages` | Pages belonging to that layout |
| `detected_table_blocks` | Number of SKU/page table blocks found |
| `signature` | Rounded coordinate signature |

If there is one layout group, draw one template.

If there are multiple layout groups, draw one template for each representative page and set each template to apply only to that group’s pages.

---

## 9. Config file reference

### 9.1 Required top-level settings

```yaml
input_pdf: "C:/Path/To/catalogue.pdf"
output_folder: "C:/Path/To/Index Output"
index_pdf_pages: "1293-1364"
page_source_of_truth: "pdf_page_number"
```

| Setting | Meaning |
|---|---|
| `input_pdf` | PDF to process |
| `output_folder` | Where outputs are written |
| `index_pdf_pages` | 1-based PDF pages to process |
| `page_source_of_truth` | Always `pdf_page_number` for this tool |

### 9.2 Required columns

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
```

The `sku` and `page` columns are mandatory.

### 9.3 Optional columns

```yaml
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
```

Optional columns do not control row alignment. The extractor first identifies product rows using SKU + page, then reads optional values from the same visual row.

Optional mismatches are warnings, not extraction errors.

### 9.4 Expected table blocks

```yaml
expected_table_blocks_per_page: 3
```

If the page has three repeated index blocks but the tool detects only two, the page is flagged.

### 9.5 Header matching

```yaml
header_matching:
  case_sensitive: false
```

If `false`, `Code`, `CODE`, and `code` are treated as equivalent.

### 9.6 SKU detection

```yaml
sku_detection:
  positive_examples:
    - "GW 21 005"
    - "GW D3 674"
    - "DX 56 225"
  negative_examples:
    - "DX 26"
    - "GW 21"
  sku_regex: "\\b(?:GW|DX)(?:[\\s\\-_/\\.]+[A-Z0-9]{1,8}){2,5}\\b"
```

At least three positive examples are required. Negative examples are used as design guidance and can also be represented in `ignore_row_patterns`.

### 9.7 SKU rules

```yaml
sku_rules:
  uppercase_only: true
  allowed_characters: "A-Z0-9 space hyphen slash dot underscore plus"
```

If `uppercase_only` is true, lowercase characters are removed from the clean SKU after candidate extraction but retained in raw evidence.

The output keeps both raw and normalized forms where relevant:

```text
sku_raw / source_sku_line_text
sku
sku_normalized
```

### 9.8 Page detection

```yaml
page_detection:
  positive_examples:
    - "344"
    - "839"
    - "12/13"
  page_regex: "(?i)\\b(?:see\\s+page\\s+)?[A-Z]?\\d+(?:\\s*(?:,|/|;|-)\\s*[A-Z]?\\d+)*\\b"
  keep_original_and_normalized: true
```

Page values are treated as text. The output keeps both original and normalized values.

Examples:

| Original | Normalized |
|---|---|
| `12/13` | `12;13` |
| `12, 13` | `12;13` |
| `A12` | `A12` |
| `See page 12` | `12` |

### 9.9 Ignore row patterns

```yaml
ignore_row_patterns:
  - "^DX\\s+\\d+$"
  - "^GW\\s+\\d+$"
```

These patterns catch section/title rows such as `DX 26` and `GW 21`. Such rows are ignored before optional columns are read, preventing values like `DX` or `26` from shifting into `pack_carton` and `pallet`.

### 9.10 Validation groups

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

Validation groups check that values came from the **same extracted row**. Optional values are checked only if supplied.

A validation group can include only required fields:

```yaml
- code: "GW 21 005"
  page: "1026"
```

or required plus optional fields:

```yaml
- code: "DX 10 016 R"
  pack_carton: "100/6400"
  pallet: "6400"
  page: "344"
```

If a validation group fails, the relevant page is treated as needing review and gets a debug image when possible.

### 9.11 Extraction mode

```yaml
extraction_mode: "auto"
```

Possible values:

| Value | Meaning |
|---|---|
| `auto` | Detect headers and build zones automatically |
| `manual` | Use `manual_coordinate_blocks` |
| `visual_template` | Use `visual_template_sets` |

### 9.12 Visual template sets

This is usually written by the visual selector or by `--visual-template`.

```yaml
visual_template_sets:
  - template_path: "C:/Path/To/layout_1_template.json"
    template_name: "layout_1_template"
    selection_pdf_page: 1312
    apply_pdf_pages: "1293-1364"
    coordinate_blocks:
      - block_number: 1
        block_x0: 15.0
        block_x1: 245.0
        data_top: 82.0
        data_bottom: 760.0
        columns:
          sku:
            x0: 15.0
            x1: 90.0
            header_text: "Code"
            alignment: "left"
            inclusion_mode: "left"
          page:
            x0: 220.0
            x1: 245.0
            header_text: "Page"
            alignment: "left"
            inclusion_mode: "left"
```

### 9.13 Debug images

```yaml
debug_images:
  enabled: true
  only_issue_pages: true
  label_zones: true
```

Debug images are generated only for issue pages. They show block boundaries, column zones, and labels such as `B1 sku`.

### 9.14 Review files

```yaml
review_files:
  excel_safe: true
  write_separate_csvs: false
```

The main extracted files are always CSV. Review information is consolidated into an Excel workbook.

### 9.15 Advanced settings

```yaml
advanced:
  header_y_tolerance: 6.0
  line_y_tolerance: 3.0
  data_start_padding: 1.0
  x_tolerance: 1
  y_tolerance: 3
  data_bottom: 760
  boundary_overlap_warning_threshold: 0.75
```

| Setting | Meaning |
|---|---|
| `header_y_tolerance` | How close headers must be vertically to be treated as the same header row |
| `line_y_tolerance` | How words are grouped into visual rows |
| `data_start_padding` | Gap below detected header before data starts |
| `x_tolerance` / `y_tolerance` | Passed to pdfplumber word extraction |
| `data_bottom` | Manual bottom cutoff for table data |
| `boundary_overlap_warning_threshold` | Controls boundary-overlap warnings |

`data_bottom` is required. It prevents footers/page numbers below the table being extracted as data.

---

## 10. Alignment and inclusion mode

### 10.1 Alignment

`alignment` describes how the column is visually aligned and helps build automatic boundaries from headers.

Options:

| Alignment | Meaning |
|---|---|
| `left` | Values start near the left edge of the column |
| `right` | Values end near the right edge of the column |
| `center` | Values are centred |
| `contained` | Strict bounding behaviour |
| `majority` | Majority-overlap behaviour |

### 10.2 Inclusion mode

`inclusion_mode` decides whether a text fragment belongs inside a column zone.

Options:

| Inclusion mode | Rule | Best for |
|---|---|---|
| `left` | Text left edge must be inside the column | Left-aligned columns |
| `right` | Text right edge must be inside the column | Right-aligned columns |
| `center` | Text centre must be inside the column | Centred values |
| `contained` | Whole text must fit inside the column | Strict extraction |
| `majority` | Most of the text must overlap the column | Balanced extraction |
| `anchor_or_overlap` | Include by anchor, but flag overlap | Review-heavy safety mode |

For Gewiss-style index pages where all values are left-aligned, use:

```yaml
alignment: "left"
inclusion_mode: "left"
```

### 10.3 Automatic boundary logic

Automatic mode uses configured headers to create column zones.

For left-aligned columns, the boundary starts at the header `x0` and ends at the next column header `x0`.

Example:

```text
Code starts at x=20
Pack/carton starts at x=100
Pallet starts at x=150
Page starts at x=220
```

The zones become:

```text
Code:        x=20  to x=100
Pack/carton: x=100 to x=150
Pallet:      x=150 to x=220
Page:        x=220 to end of block
```

This prevents the SKU column from swallowing the next column's `10/200` value when the SKU is visually `GW 10 195 AB`.

If any involved column is centre-aligned, the script prompts for manual coordinates or uses visual template mode because centre alignment cannot be safely inferred from header x0 alone.

---

## 11. Row-first extraction logic

The current extractor uses row-first extraction.

For each table block:

```text
1. Group PDF words into visual rows.
2. For each row, read the SKU column.
3. For the same row, read the Page column.
4. If both SKU and Page exist, create a confirmed product row.
5. Read optional columns from that same row only.
6. If optional values fail validation, blank/warn them without shifting rows.
```

This avoids the old problem where a title row like `DX 26` could cause:

```text
pack_carton = DX
pallet = 26
```

because optional columns no longer create row alignment.

---

## 12. Raw SKU-column audit

The raw audit scans only the selected/detected SKU column zones, not the full page.

This means:

```text
Raw SKU not in structured
```

now means:

```text
A SKU-like candidate was found inside the SKU/Product Code column zone but was not confirmed as a structured SKU/page row.
```

It no longer includes SKU-like text from notes, captions, descriptions, logos, or unrelated areas outside the SKU column.

---

## 13. Outputs

### 13.1 Main extracted CSVs

```text
sku_registry.csv
index_rows.csv
```

`sku_registry.csv` contains one row per unique SKU. Use this as the input to the next product extraction stage.

`index_rows.csv` contains row-level SKU/page occurrences and optional column values.

### 13.2 Review workbook

```text
extraction_review_workbook.xlsx
```

Sheets:

| Sheet | Purpose |
|---|---|
| Run Summary | Counts and high-level status |
| Page Diagnostics | One row per processed page with issues/warnings |
| Index Rows Review | Row-level extraction details |
| Unresolved Rows | Rows needing review |
| Raw SKU Column Audit | SKU candidates found inside SKU column zones |
| Header Detection | Header locations detected/validated |
| Validation Groups | Results for supplied validation groups |
| Extractor Errors | Errors raised during processing |

### 13.3 Debug images

```text
debug_images/
```

Images are created only for pages with required issues or raw audit issues. They show detected or selected column zones.

### 13.4 Config used

```text
catalogue_index_config_used.yaml
```

This preserves the exact settings used for the run.

---

## 14. How to review the output

### Step 1 — Open `Run Summary`

Check:

| Metric | Desired result |
|---|---|
| `sku_registry_rows` | Should be close to expected catalogue SKU count |
| `unresolved_rows` | Ideally low, but not necessarily zero |
| `pages_needing_review` | Reviewable count |
| `extractor_errors` | Should be zero |

### Step 2 — Open `Page Diagnostics`

Key columns:

| Column | Meaning |
|---|---|
| `status` | `ok` or `needs_review` |
| `detected_table_blocks` | Blocks found/used |
| `structured_rows` | Confirmed + review rows |
| `required_issues` | SKU/page/block issues |
| `optional_warnings` | Optional column warnings only |
| `raw_audit_issues` | SKU-column audit issues |
| `debug_image_path` | Image to inspect |

Issue cells use line breaks for readability.

### Step 3 — Open `Validation Groups`

Every configured validation group should be `found`.

If a group fails, it means the exact row combination was not extracted correctly.

### Step 4 — Open debug image for issue pages

Check whether:

- the `sku` region covers only product codes;
- the `page` region covers only page numbers;
- the bottom cutoff excludes the printed page footer;
- headers are inside selected boxes if using visual template mode.

### Step 5 — Inspect `Raw SKU Column Audit`

If a raw audit candidate is not in structured rows, check whether:

- the row is missing a page value;
- the SKU regex is too broad;
- the selected SKU region includes non-product text;
- a title row is not covered by `ignore_row_patterns`.

---

## 15. Troubleshooting examples

### Problem: `AB 10/200` appears in `pack_carton`

Likely cause: column boundaries or row grouping included the SKU suffix and the next column value together.

Fixes:

- use row-first v3/v4 logic;
- set `pack_carton.value_regex` to numeric/slash only;
- use `alignment: left` and `inclusion_mode: left`;
- use visual template mode to draw exact pack/carton boundaries.

### Problem: printed index page number is read as a page value

Likely cause: table bottom cutoff includes the footer.

Fix:

```yaml
advanced:
  data_bottom: 760
```

or draw visual regions so their bottom edge stops above the footer.

### Problem: `DX 26` becomes optional column data

Likely cause: section/title rows are being read as table data.

Fix:

```yaml
ignore_row_patterns:
  - "^DX\\s+\\d+$"
  - "^GW\\s+\\d+$"
```

### Problem: one visual template does not work on all pages

Run:

```powershell
py catalogue_index_extractor.py --config config.yaml --scan-layouts
```

If multiple layout groups appear, draw one template per group and apply each to its corresponding pages.

---

## 16. Visual-template multi-layout example

If the scanner finds two layouts:

```text
layout_1 pages: 1293-1320
layout_2 pages: 1321-1364
```

Draw two templates:

```powershell
py catalogue_region_selector.py --pdf catalogue.pdf --config config.yaml --page 1293 --output-template layout_1.json --output-config config_visual_1.yaml --apply-pages "1293-1320"

py catalogue_region_selector.py --pdf catalogue.pdf --config config_visual_1.yaml --page 1321 --output-template layout_2.json --output-config config_visual_2.yaml --apply-pages "1321-1364"
```

Then run:

```powershell
py catalogue_index_extractor.py --config config_visual_2.yaml
```

---

## 17. Acceptance checklist

A good run should have:

```text
sku_registry.csv generated
index_rows.csv generated
extraction_review_workbook.xlsx generated
extractor_errors = 0
validation groups found
issue pages reviewed using debug images
no unexplained raw SKU-column audit misses
```

---

## 18. Summary

The extractor now has three complementary modes:

```text
auto            = fast header-based extraction
manual          = terminal coordinate entry
visual_template = user-drawn boxes on a PDF preview
```

The core extraction logic remains shared and stable:

```text
PDF words with coordinates
→ table blocks / selected visual regions
→ row-first SKU + Page extraction
→ optional column extraction from the same row
→ optional validation
→ SKU-column-only raw audit
→ review workbook + debug images
→ one-row-per-SKU registry
```
