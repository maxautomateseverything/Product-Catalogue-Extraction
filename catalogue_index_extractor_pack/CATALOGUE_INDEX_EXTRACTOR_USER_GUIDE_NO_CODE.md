# Catalogue Index Extractor — Non-Technical User Guide

**Audience:** This guide is written for a user with no coding experience. It explains what the extractor does, which catalogue index layouts it can handle, which extraction mode to use, which files to open, which files to edit, which commands to run, and how to review the results.

---

## 1. What this tool does

The Catalogue Index Extractor reads selected PDF catalogue index pages and produces a clean list of product codes and their catalogue page references.

The main output is:

```text
sku_registry.csv
```

This file contains one row per unique product code/SKU and the page or pages where that product appears.

The extractor is designed for **selectable-text PDFs**. This means you should be able to open the PDF and highlight/copy the product code text with your mouse. If the PDF is a scanned image and the text cannot be selected, this tool will not work reliably unless an OCR process is added separately.

---

## 2. Important terms

| Term | Meaning |
|---|---|
| **PDF page** | The page number inside the PDF file. This is the page number the script uses to open the PDF. |
| **Index page** | A page in the catalogue that lists product codes and page references. |
| **Product Code / SKU** | The product code to extract. Examples: `GW 21 005`, `RTW0330C-24`, `RVC4K675`. |
| **Catalogue Page / Product Page** | The page value printed in the index row that tells you where the product appears. |
| **Table block** | One repeated table on a page. Some index pages have 3 separate blocks across the page. |
| **Required columns** | The minimum columns needed: product code/SKU and page. |
| **Optional columns** | Extra columns such as `Pack/carton`, `Pallet`, `Trade Price`, etc. |
| **Config file** | A YAML file where you tell the extractor what PDF to use, which pages to process, and what settings to apply. |
| **Visual template** | A saved set of boxes drawn over the PDF page to define where each column is. |
| **Coordinate blocks** | The actual coordinates created by the visual selector. These are what the extractor uses to read text from selected boxes. |
| **Debug image** | An image created for issue pages showing the detected or drawn boxes. |

---

## 3. Files included in the extractor pack

Your working folder should contain files similar to this:

```text
catalogue_index_extractor.py
catalogue_region_selector.py
CATALOGUE_INDEX_EXTRACTOR_GUIDE.md
requirements.txt
example_config_gewiss_style.yaml
```

### What each file is for

| File | What it does | Do you edit it? |
|---|---|---|
| `catalogue_index_extractor.py` | Main extractor. Reads the PDF and creates outputs. | No. |
| `catalogue_region_selector.py` | Visual selector. Lets you draw boxes around columns. | No. |
| `requirements.txt` | List of Python packages needed. | No. |
| `example_config_gewiss_style.yaml` | Example config you can copy and edit. | Copy it, then edit the copy. |
| Your own config, e.g. `config_robus_style.yaml` | Your catalogue-specific settings. | Yes. |

---

## 4. First-time setup

Open PowerShell in the extractor pack folder.

Example folder:

```powershell
cd "C:\Users\max.yoong\OneDrive - Edmundson Electrical Ltd\Product Data - Documents\1 - Pentaho\X - Max Yoong\7. Catalogue\catalogue_index_extractor_pack"
```

Install the required packages:

```powershell
py -m pip install -r requirements.txt
```

You usually only need to do this once.

---

## 5. The three extraction modes

The extractor has three main modes.

| Mode | Config value | When to use it |
|---|---|---|
| Automatic header mode | `auto` | The index pages have column headers such as `Code`, `Page`, `Pack/carton`, and the layout is consistent. |
| Manual terminal coordinate mode | `manual` | You know the exact PDF x/y coordinates and want to type them manually. |
| Visual template mode | `visual_template` | There are no headers, headers are unreliable, columns are shifted, or you want to draw the column boxes visually. |

Most non-technical users will use either:

```yaml
extraction_mode: "auto"
```

or:

```yaml
extraction_mode: "visual_template"
```

---

## 6. Decision guide — which mode should I use?

### Question 1 — Do the index tables have column headers?

Examples of headers:

```text
Code | Pack/carton | Pallet | Page
Product Code | Page
SKU | Pg
```

If yes, continue to Question 2.

If no, use **Visual Template Mode**.

---

### Question 2 — Are the headers in the same place on all index pages?

If yes, use **Automatic Header Mode**.

If no, use **Layout Scan + Visual Template Mode**.

---

### Question 3 — Are there different page layouts?

Examples:

- Some pages are shifted left.
- Some pages are shifted right.
- Some pages have 2 table blocks, others have 3.
- Some pages have optional columns and others do not.

If yes, create **one visual template per layout**.

---

### Question 4 — Is the PDF scanned?

If the text cannot be highlighted/copied in the PDF, the PDF is likely scanned or image-only.

This extractor is not designed for that case.

---

## 7. Catalogue index types the extractor can handle

## Type A — Headered index, one consistent layout

### Example layout

```text
Code        Pack/carton    Pallet    Page
GW 10 195 AB    10/200      200      785
GW 10 196 AB    10/200      200      785
```

### Recommended mode

Use:

```yaml
extraction_mode: "auto"
```

### Why

The script can find the `Code` and `Page` headers automatically, build column zones, and extract rows.

### Config example

```yaml
input_pdf: "C:/Path/To/catalogue.pdf"
output_folder: "C:/Path/To/Index Output"
index_pdf_pages: "1293-1364"

extraction_mode: "auto"

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
```

### Command to run

```powershell
py catalogue_index_extractor.py --config "C:\Path\To\config.yaml"
```

---

## Type B — Headered index with repeated blocks across each page

### Example layout

```text
Code | Page        Code | Page        Code | Page
A001 | 12          B001 | 35          C001 | 78
A002 | 13          B002 | 36          C002 | 79
```

### Recommended mode

Use:

```yaml
extraction_mode: "auto"
expected_table_blocks_per_page: 3
```

### Why

The script detects each `Code` and `Page` header pair and treats each pair as one table block.

### Important setting

```yaml
expected_table_blocks_per_page: 3
```

If one page has a different number of blocks, the page will be flagged for review.

---

## Type C — Headered index where layouts change between pages

### Example

Pages 100-120 have three blocks.

Pages 121-130 have two blocks.

Pages 131-140 have three blocks but shifted right.

### Recommended approach

First run the layout scanner:

```powershell
py catalogue_index_extractor.py --config "C:\Path\To\config.yaml" --scan-layouts
```

This creates layout review files in the output folder.

Look for:

```text
layout_scan_review.csv
layout_scan_summary.csv
layout_scan_summary.json
```

### What to do next

If the scanner shows one layout only, use `auto` mode.

If it shows multiple layouts, use visual templates for the pages that need them.

---

## Type D — Headerless index with product code and page columns

This is the case where the page contains product codes and page values, but no column headers.

### Example layout

```text
RTW0330C-24     378        RVC4K675      112        RAAS       52
RTW0340C-24     379        RVC4K840      113        RAAS2      53
```

The page may visually contain 3 tables/blocks, each with 2 columns:

```text
Block 1: Product Code | Page
Block 2: Product Code | Page
Block 3: Product Code | Page
```

### Recommended mode

Use:

```yaml
extraction_mode: "visual_template"
```

### Why

There are no headers, so automatic header detection cannot work.

You must draw the SKU and Page boxes yourself.

### What to draw

For each page layout, draw six boxes:

```text
block 1 sku
block 1 page
block 2 sku
block 2 page
block 3 sku
block 3 page
```

### Important config settings

```yaml
required_columns:
  sku:
    header_text: "NO_HEADER_SKU"
    alignment: "left"
    inclusion_mode: "left"

  page:
    header_text: "NO_HEADER_PAGE"
    alignment: "left"
    inclusion_mode: "left"

optional_columns: []
expected_table_blocks_per_page: 3

visual_template:
  require_headers_inside_regions: false
  start_data_below_header: false
```

Because there are no headers, both of these must be false:

```yaml
require_headers_inside_regions: false
start_data_below_header: false
```

---

## Type E — Headerless index with shifted left/right layouts

### Example

Pages 590, 592, 594, 596 use a left-shifted layout.

Pages 591, 593, 595 use a right-shifted layout.

Each page has 3 blocks, and each block has 2 columns.

### Recommended approach

Use **two visual templates**:

```text
template_headerless_left.json
template_headerless_right.json
```

Then apply each template to the correct pages.

### Example visual template sets

```yaml
visual_template_sets:
  - template_path: "C:/Path/To/template_headerless_left.json"
    apply_pdf_pages: "590,592,594,596"
    coordinate_blocks:
      # created by the selector

  - template_path: "C:/Path/To/template_headerless_right.json"
    apply_pdf_pages: "591,593,595"
    coordinate_blocks:
      # created by the selector
```

Important: The extractor needs the actual `coordinate_blocks`. Do not only type `template_path` manually unless your current script version loads templates directly. The safest process is to let the visual selector create the config.

---

## Type F — Index with optional columns

### Example layout

```text
Code          Pack/carton     Pallet     Page
DX 10 016 R   100/6400        6400       344
GW 15 415     1/12                       839
```

### Recommended mode

Use `auto` if headers are present and reliable.

Use `visual_template` if the boundaries are difficult.

### Optional column validation

If an optional column should only contain certain values, add `value_regex`.

Example:

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

This rejects values such as:

```text
DX
GW
AB 10/200
DX 26
```

but keeps the raw evidence in review outputs.

---

## Type G — Index pages with title rows inside the table

### Example

```text
DX 26
DX 26 101      344
DX 26 102      345
```

The row `DX 26` is a title row, not a product code row.

### Recommended settings

Add ignore row patterns:

```yaml
ignore_row_patterns:
  - "^DX\\s+\\d+$"
  - "^GW\\s+\\d+$"
```

This tells the extractor not to treat title rows like product rows.

---

## Type H — Index pages with printed footer page numbers

Sometimes the printed page number at the bottom of the index page is inside the same area as the far-right Page column.

### Problem

The extractor may read the footer as a page value.

### Fix in auto mode

Set a manual bottom cutoff:

```yaml
advanced:
  data_bottom: 760
```

Adjust the number by looking at the debug image.

### Fix in visual template mode

Draw the Page boxes so they stop at the last real row and do not include the footer.

---

## 8. Page range formats

You can process one range:

```yaml
index_pdf_pages: "590-596"
```

You can skip a page in the middle:

```yaml
index_pdf_pages: "590-592,594-596"
```

You can list individual pages:

```yaml
index_pdf_pages: "590,592,594,596"
```

You can mix ranges and individual pages:

```yaml
index_pdf_pages: "590-592,594,596"
```

Always put the value in quotes.

---

## 9. Automatic header mode — full workflow

Use this workflow when your index pages have clear column headers.

### Step 1 — Copy the example config

Make a copy of:

```text
example_config_gewiss_style.yaml
```

Rename it to something specific, for example:

```text
config_supplier_auto.yaml
```

### Step 2 — Edit the config

Open your config in Notepad, VS Code, or another text editor.

Edit:

```yaml
input_pdf: "C:/Path/To/catalogue.pdf"
output_folder: "C:/Path/To/Index Output"
index_pdf_pages: "1293-1364"
```

Set:

```yaml
extraction_mode: "auto"
```

Set the required headers:

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

### Step 3 — Run the extractor

```powershell
py catalogue_index_extractor.py --config "C:\Path\To\config_supplier_auto.yaml"
```

### Step 4 — Review outputs

Open the output folder and review:

```text
sku_registry.csv
index_rows.csv
extraction_review_workbook.xlsx
```

---

## 10. Visual template mode — full workflow

Use this workflow when:

- the index has no headers;
- the headers are not reliable;
- the columns move between pages;
- you want to manually draw the exact extraction boxes.

---

## 10.1 Create a base config

Create a config file, for example:

```text
config_robus_style.yaml
```

Example for a headerless two-column index:

```yaml
input_pdf: "C:/Path/To/ROBUS UK & IRE 2026 CATALOGUE.pdf"
output_folder: "C:/Path/To/Headerless Index Output"
index_pdf_pages: "590-596"

page_source_of_truth: "pdf_page_number"
extraction_mode: "visual_template"

required_columns:
  sku:
    header_text: "NO_HEADER_SKU"
    alignment: "left"
    inclusion_mode: "left"

  page:
    header_text: "NO_HEADER_PAGE"
    alignment: "left"
    inclusion_mode: "left"

optional_columns: []
expected_table_blocks_per_page: 3

header_matching:
  case_sensitive: false

sku_detection:
  positive_examples:
    - "RTW0330C-24"
    - "RVC4K675"
    - "RAAS"
  negative_examples: []
  sku_regex: "\\S(?:.*\\S)?"

sku_rules:
  uppercase_only: true
  remove_lowercase_from_clean_sku: true
  preserve_raw_sku: true
  allowed_characters_regex: "[A-Z0-9\\s\\-_/\\.]"

page_detection:
  positive_examples: []
  page_regex: ""
  keep_original_and_normalized: true

ignore_row_patterns: []

example_validation_groups:
  - code: "RTW0330C-24"
    page: "378"

  - code: "RVC4K675"
    page: "112"

visual_template:
  require_headers_inside_regions: false
  start_data_below_header: false

visual_template_sets: []

debug_images:
  enabled: true
  only_issue_pages: true

review_files:
  excel_safe: true
  write_separate_csvs: false

advanced:
  line_y_tolerance: 3.0
  data_bottom: 9999
  x_tolerance: 1
  y_tolerance: 3
```

---

## 10.2 Open the visual selector

Run the selector for a representative page.

Example left-shift page:

```powershell
py catalogue_region_selector.py `
  --pdf "C:\Path\To\ROBUS UK & IRE 2026 CATALOGUE.pdf" `
  --config "config_robus_style.yaml" `
  --page 590 `
  --zoom 2.0 `
  --output-template "C:\Path\To\template_headerless_left.json" `
  --output-config "config_robus_style_left_added.yaml" `
  --apply-pages "590,592,594,596"
```

If PowerShell does not like the multi-line version, use a single line:

```powershell
py catalogue_region_selector.py --pdf "C:\Path\To\ROBUS UK & IRE 2026 CATALOGUE.pdf" --config "config_robus_style.yaml" --page 590 --zoom 2.0 --output-template "C:\Path\To\template_headerless_left.json" --output-config "config_robus_style_left_added.yaml" --apply-pages "590,592,594,596"
```

---

## 10.3 Draw the boxes

For a page with 3 blocks and 2 columns per block, draw:

```text
block 1 sku
block 1 page
block 2 sku
block 2 page
block 3 sku
block 3 page
```

Each box should cover only the relevant data column.

Do not include unrelated page text, footers, or neighbouring columns.

---

## 10.4 Save the template and generated config

The selector should create:

```text
template_headerless_left.json
config_robus_style_left_added.yaml
```

The generated config should contain:

```yaml
coordinate_blocks:
```

This is important.

If the generated config does not contain `coordinate_blocks`, the extractor will not know where the boxes are.

---

## 10.5 Create the second template if there is another layout

If pages are shifted right, run the selector again using the config created from the first run.

Example:

```powershell
py catalogue_region_selector.py `
  --pdf "C:\Path\To\ROBUS UK & IRE 2026 CATALOGUE.pdf" `
  --config "config_robus_style_left_added.yaml" `
  --page 591 `
  --zoom 2.0 `
  --output-template "C:\Path\To\template_headerless_right.json" `
  --output-config "config_robus_style_visual_final.yaml" `
  --apply-pages "591,593,595"
```

Now use this final config for extraction:

```text
config_robus_style_visual_final.yaml
```

---

## 10.6 Run the extractor with the final visual config

```powershell
py catalogue_index_extractor.py --config "config_robus_style_visual_final.yaml"
```

You should see terminal output like:

```text
Page 1/7: PDF page 590 done - blocks=3, rows=...
Page 2/7: PDF page 591 done - blocks=3, rows=...
```

If you see:

```text
blocks=0, rows=0
```

then the config probably does not contain coordinate blocks, or the page numbers in `apply_pdf_pages` do not match the index pages being processed.

---

## 11. Config reference

This section explains the main settings.

---

## 11.1 `input_pdf`

The full path to the catalogue PDF.

Example:

```yaml
input_pdf: "C:/Path/To/catalogue.pdf"
```

Use forward slashes `/` or double backslashes `\\` in YAML paths.

---

## 11.2 `output_folder`

The folder where outputs will be saved.

Example:

```yaml
output_folder: "C:/Path/To/Index Output"
```

The folder will be created if it does not already exist.

---

## 11.3 `index_pdf_pages`

The PDF pages to process.

Examples:

```yaml
index_pdf_pages: "590-596"
index_pdf_pages: "590,592,594,596"
index_pdf_pages: "590-592,594-596"
```

---

## 11.4 `extraction_mode`

Accepted values:

```yaml
extraction_mode: "auto"
extraction_mode: "manual"
extraction_mode: "visual_template"
```

Use `auto` when headers exist.

Use `visual_template` when you need to draw boxes.

---

## 11.5 `required_columns`

The extractor needs at least two required fields:

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

For headerless visual templates, use placeholder names:

```yaml
header_text: "NO_HEADER_SKU"
header_text: "NO_HEADER_PAGE"
```

---

## 11.6 `alignment`

Alignment describes how the column text is visually aligned.

Common values:

| Value | Meaning | Use when |
|---|---|---|
| `left` | Text starts at the left side of the column. | Product codes, most text columns. |
| `right` | Text ends at the right side of the column. | Prices, numeric values, page numbers in some catalogues. |
| `center` | Text is centred. | Rare. Use carefully. |

Alignment helps the script decide boundaries when using header-based extraction.

---

## 11.7 `inclusion_mode`

Inclusion mode controls how the extractor decides whether a text item belongs inside a column.

| Value | Meaning | Best for |
|---|---|---|
| `left` | Include text if its left edge starts inside the column. | Left-aligned columns. |
| `right` | Include text if its right edge ends inside the column. | Right-aligned columns. |
| `center` | Include text if its centre is inside the column. | Centred columns. |
| `contained` | Include text only if the whole text is inside the column. | Strict extraction. |
| `majority` | Include text if most of it is inside the column. | Balanced extraction. |

For most product code columns, use:

```yaml
inclusion_mode: "left"
```

---

## 11.8 `optional_columns`

Optional columns are extra fields. They do not control whether a product row is valid.

Example:

```yaml
optional_columns:
  - output_name: "pack_carton"
    header_text: "Pack/carton"
    alignment: "left"
    inclusion_mode: "left"
    value_regex: "^[0-9]+(?:/[0-9]+)*$"
    invalid_value_action: "blank_and_warn"
```

If there are no optional columns:

```yaml
optional_columns: []
```

---

## 11.9 `sku_detection`

This controls how SKU text is detected inside the SKU column.

For normal pattern-based extraction:

```yaml
sku_regex: "\\b(?:GW|DX)(?:[\\s\\-_/\\.]+[A-Z0-9]{1,8}){2,5}\\b"
```

For visual selector mode where the SKU box contains only product codes:

```yaml
sku_regex: "\\S(?:.*\\S)?"
```

This means:

```text
Take the full non-empty line inside the selected SKU region.
```

---

## 11.10 `sku_rules`

Example:

```yaml
sku_rules:
  uppercase_only: true
  remove_lowercase_from_clean_sku: true
  preserve_raw_sku: true
  allowed_characters_regex: "[A-Z0-9\\s\\-_/\\.]"
```

| Setting | Meaning |
|---|---|
| `uppercase_only` | Treat uppercase product codes as the valid form. |
| `remove_lowercase_from_clean_sku` | Remove lowercase characters from the clean SKU. Useful when symbols are misread as lowercase letters. |
| `preserve_raw_sku` | Keep the original raw text as evidence. |
| `allowed_characters_regex` | Defines which characters can remain in the cleaned SKU. |

---

## 11.11 `page_detection`

For most page columns:

```yaml
page_detection:
  positive_examples: []
  page_regex: ""
  keep_original_and_normalized: true
```

A blank page regex means:

```text
Keep the non-empty text found in the Page column.
```

---

## 11.12 `ignore_row_patterns`

Use this to ignore title rows.

Example:

```yaml
ignore_row_patterns:
  - "^DX\\s+\\d+$"
  - "^GW\\s+\\d+$"
```

---

## 11.13 `example_validation_groups`

Use this to check known correct rows.

Example with only SKU and page:

```yaml
example_validation_groups:
  - code: "RTW0330C-24"
    page: "378"
```

Example with optional columns:

```yaml
example_validation_groups:
  - code: "DX 10 016 R"
    pack_carton: "100/6400"
    pallet: "6400"
    page: "344"
```

Validation groups check that the values were found on the same extracted row.

---

## 11.14 `visual_template`

For headerless visual templates:

```yaml
visual_template:
  require_headers_inside_regions: false
  start_data_below_header: false
```

For visual templates that include headers:

```yaml
visual_template:
  require_headers_inside_regions: true
  start_data_below_header: true
```

---

## 11.15 `visual_template_sets`

This tells the extractor which template applies to which pages.

Example:

```yaml
visual_template_sets:
  - template_path: "C:/Path/To/template_left.json"
    apply_pdf_pages: "590,592,594,596"
    coordinate_blocks:
      # created by selector

  - template_path: "C:/Path/To/template_right.json"
    apply_pdf_pages: "591,593,595"
    coordinate_blocks:
      # created by selector
```

The safest process is to let `catalogue_region_selector.py` create this section.

---

## 11.16 `debug_images`

```yaml
debug_images:
  enabled: true
  only_issue_pages: true
```

Debug images are useful for seeing whether the boxes are in the right place.

---

## 11.17 `review_files`

```yaml
review_files:
  excel_safe: true
  write_separate_csvs: false
```

`excel_safe: true` helps stop Excel from converting product codes into dates or numbers in review outputs.

The main data files remain normal UTF-8 CSVs.

---

## 11.18 `advanced`

Example:

```yaml
advanced:
  line_y_tolerance: 3.0
  data_bottom: 9999
  x_tolerance: 1
  y_tolerance: 3
```

| Setting | Meaning |
|---|---|
| `line_y_tolerance` | How close text must be vertically to count as the same row. |
| `data_bottom` | Bottom y-coordinate cutoff for extraction. Useful to exclude footers. |
| `x_tolerance` | PDF word extraction tolerance. Usually leave as default. |
| `y_tolerance` | PDF word extraction tolerance. Usually leave as default. |

---

## 12. Output files

After extraction, open the output folder.

Typical outputs:

```text
sku_registry.csv
index_rows.csv
extraction_review_workbook.xlsx
catalogue_index_config_used.yaml
debug_images/
```

---

## 12.1 `sku_registry.csv`

This is the main output.

It contains one row per unique SKU.

Important columns:

| Column | Meaning |
|---|---|
| `sku` | Product code as extracted. |
| `sku_normalized` | Product code cleaned for matching. |
| `catalogue_pages_original` | Original page values found in the index. |
| `catalogue_pages_normalized` | Normalized page values. |
| `source_pdf_pages` | PDF index pages where the SKU was found. |
| `source_table_blocks` | Which table block the SKU came from. |
| `source_methods` | How the SKU was extracted. |
| `confidence_status` | `confirmed` or `needs_review`. |
| `review_reason` | Why review is needed. |
| `occurrence_count` | Number of times the SKU appeared. |

---

## 12.2 `index_rows.csv`

This shows every extracted row before grouping into unique SKUs.

Use this when checking a specific product code/page pair.

---

## 12.3 `extraction_review_workbook.xlsx`

This is the main review workbook.

Expected sheets may include:

| Sheet | Purpose |
|---|---|
| `Run Summary` | Overall run counts and status. |
| `Page Diagnostics` | Page-by-page issues and warnings. |
| `Index Rows Review` | Every extracted row with review information. |
| `Unresolved Rows` | Rows that need checking. |
| `Raw SKU Column Audit` | SKU-like text found in SKU regions. |
| `Header Detection` | Headers found during auto mode. |
| `Validation Groups` | Results of known example checks. |
| `Visual Templates` | Template boxes used during extraction. |
| `Extractor Errors` | Any technical errors. |

---

## 12.4 `debug_images/`

This folder contains images for issue pages.

Use these to check:

- Are boxes drawn over the correct columns?
- Are boxes too wide?
- Are boxes too narrow?
- Do boxes include footer text?
- Are the correct templates applied to the correct pages?

---

## 13. How to review a run

### Step 1 — Check terminal output

Good visual-template output should show blocks and rows:

```text
blocks=3, rows=120
```

Bad output:

```text
blocks=0, rows=0
```

If blocks are zero, the extractor did not load usable coordinates for that page.

---

### Step 2 — Open `sku_registry.csv`

Check:

- Are expected SKUs present?
- Do page values look correct?
- Are there obvious non-SKU values?

---

### Step 3 — Open `extraction_review_workbook.xlsx`

Start with:

```text
Run Summary
Page Diagnostics
Validation Groups
Unresolved Rows
```

---

### Step 4 — Review debug images

If a page has issues, open its debug image.

A good debug image should show boxes over the expected columns.

If no boxes are shown in visual mode, your config likely does not contain `coordinate_blocks`.

---

## 14. Common problems and fixes

## Problem: `blocks=0, rows=0`

### Likely cause

The visual template was not converted into coordinate blocks, or the page is not included in `apply_pdf_pages`.

### Fix

Open the final config and search for:

```yaml
coordinate_blocks:
```

If missing, rerun the visual selector and use the generated output config.

---

## Problem: Debug images show no boxes

### Likely cause

No coordinate blocks were loaded.

### Fix

Use the selector-generated config, not only a manually edited config with `template_path`.

---

## Problem: SKU includes page number

Example:

```text
RTW0330C-24 378
```

### Cause

The SKU box is too wide and includes the Page column.

### Fix

Redraw the SKU box narrower.

---

## Problem: Page values missing

### Cause

The Page box is too narrow or does not cover all page values.

### Fix

Redraw the Page box wider or taller.

---

## Problem: Footer page number extracted

### Cause

The Page box extends too far down.

### Fix

Redraw the box so it stops before the footer.

---

## Problem: Left-shift pages work but right-shift pages fail

### Cause

The wrong template is applied to some pages.

### Fix

Check `visual_template_sets`:

```yaml
apply_pdf_pages: "590,592,594,596"
```

Make sure each page uses the correct template.

---

## Problem: A title row is extracted as data

### Fix

Add an ignore pattern:

```yaml
ignore_row_patterns:
  - "^DX\\s+\\d+$"
  - "^GW\\s+\\d+$"
```

---

## Problem: Optional column contains wrong text

Example:

```text
pack_carton = AB 10/200
```

### Fixes

1. Check the column boundary.
2. Use optional column validation regex.
3. Review the raw value in the workbook.

---

## 15. Recommended approach by catalogue type

| Catalogue index type | Recommended mode | Key action |
|---|---|---|
| Headers present, same layout | `auto` | Configure header names. |
| Headers present, repeated blocks | `auto` | Set `expected_table_blocks_per_page`. |
| Headers present, multiple layouts | `auto` plus layout scan, or `visual_template` | Scan layouts, create templates if needed. |
| No headers, fixed layout | `visual_template` | Draw one template. |
| No headers, left/right shifted layouts | `visual_template` | Draw one template per layout. |
| Optional columns present | `auto` or `visual_template` | Add optional column definitions and validators. |
| Title rows inside tables | Any | Add `ignore_row_patterns`. |
| Footer page numbers interfering | Any | Use `data_bottom` or draw boxes shorter. |
| Scanned/image-only PDF | Not supported reliably | OCR would be needed. |

---

## 16. Best practices

1. Process one catalogue at a time.
2. Start with a small page range if testing.
3. Always add at least two validation groups.
4. Use visual templates when there are no headers.
5. Create one template per distinct layout.
6. Keep boxes tight but not so tight that values are cut off.
7. Do not include footers in boxes.
8. Review debug images when results look wrong.
9. Keep the final config used for each successful run.
10. Use `sku_registry.csv` as the main downstream file.

---

## 17. Minimal checklist for a headerless visual-template catalogue

Use this checklist for catalogues like Robus where pages 590-596 contain headerless index tables.

```text
[ ] Create base config.
[ ] Set extraction_mode to visual_template.
[ ] Set placeholder headers NO_HEADER_SKU and NO_HEADER_PAGE.
[ ] Set require_headers_inside_regions to false.
[ ] Set start_data_below_header to false.
[ ] Identify distinct page layouts.
[ ] Draw one visual template per layout.
[ ] Save selector-generated config.
[ ] Confirm final config contains coordinate_blocks.
[ ] Confirm apply_pdf_pages matches the intended pages.
[ ] Run catalogue_index_extractor.py with the final config.
[ ] Check terminal output: blocks should not be zero.
[ ] Open sku_registry.csv.
[ ] Open extraction_review_workbook.xlsx.
[ ] Review validation groups.
[ ] Review debug images if needed.
```

---

## 18. Example: Robus-style headerless index

### Situation

```text
PDF pages: 590-596
No column headers
3 table blocks per page
Each block has Product Code and Page columns
Two layouts: left-shifted and right-shifted
```

### Use this mode

```yaml
extraction_mode: "visual_template"
```

### Create left template

```powershell
py catalogue_region_selector.py --pdf "C:\Path\To\ROBUS UK & IRE 2026 CATALOGUE.pdf" --config "config_robus_style.yaml" --page 590 --zoom 2.0 --output-template "C:\Path\To\template_headerless_left.json" --output-config "config_robus_style_left_added.yaml" --apply-pages "590,592,594,596"
```

Draw:

```text
block 1 sku
block 1 page
block 2 sku
block 2 page
block 3 sku
block 3 page
```

### Create right template

```powershell
py catalogue_region_selector.py --pdf "C:\Path\To\ROBUS UK & IRE 2026 CATALOGUE.pdf" --config "config_robus_style_left_added.yaml" --page 591 --zoom 2.0 --output-template "C:\Path\To\template_headerless_right.json" --output-config "config_robus_style_visual_final.yaml" --apply-pages "591,593,595"
```

Draw the same six boxes.

### Run final extraction

```powershell
py catalogue_index_extractor.py --config "config_robus_style_visual_final.yaml"
```

### Expected result

Terminal should show:

```text
blocks=3, rows=...
```

Main output:

```text
sku_registry.csv
```

---

## 19. Final notes

The most important rule is:

```text
The extractor can only extract from areas it can identify.
```

For headered indexes, it identifies areas using headers.

For headerless indexes, you identify areas by drawing boxes.

When something goes wrong, check these in order:

1. Did the config process the right PDF pages?
2. Did the correct mode run?
3. Are coordinate blocks present for visual mode?
4. Were the boxes applied to the right pages?
5. Do the debug images show boxes in the right place?
6. Do validation groups pass?
7. Does `sku_registry.csv` contain the expected SKUs?

