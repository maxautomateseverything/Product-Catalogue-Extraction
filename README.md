# Product Catalogue Extraction

A Python toolkit for extracting structured product-navigation data from selectable-text PDF catalogues.

This repository currently contains two complementary extraction packs:

1. **Catalogue Index Extractor**
   Extracts product codes / SKUs and their catalogue page references from product-code index pages.

2. **Catalogue Contents Extractor**
   Extracts structured hierarchy rows from catalogue contents pages, such as category, subcategory, product range, and page number.

Together, these tools help turn large supplier catalogue PDFs into structured CSV and Excel outputs that can be reviewed, validated, and used for downstream product-data workflows.

---

## What this project is for

Supplier catalogues often contain useful product navigation data, but the information is usually locked inside PDF pages. This project helps extract that data into clean, reviewable files.

Typical use cases include:

* Building a SKU-to-page registry from catalogue index pages
* Extracting category and subcategory structures from contents pages
* Creating review workbooks for manual checking
* Producing reusable CSV outputs for later product-table extraction
* Supporting multiple catalogue layouts using config-driven rules and visual region selection

The project is designed for **selectable-text PDFs**. If the PDF is scanned/image-only and you cannot highlight or copy text in a PDF viewer, extraction will not be reliable unless OCR is added before using these tools.

---

## Repository structure

```text
Product-Catalogue-Extraction/
├── catalogue_index_extractor_pack/
│   ├── catalogue_index_extractor.py
│   ├── catalogue_region_selector.py
│   ├── CATALOGUE_INDEX_EXTRACTOR_GUIDE.md
│   ├── CATALOGUE_INDEX_EXTRACTOR_USER_GUIDE_NO_CODE.md
│   ├── example_config_gewiss_style.yaml
│   ├── config_gewiss_style.yaml
│   ├── config_robus_style.yaml
│   ├── config_robus_style_left_added.yaml
│   ├── config_robus_style_visual_final.yaml
│   └── requirements.txt
│
├── catalogue_contents_extractor_pack/
│   ├── catalogue_contents_extractor.py
│   ├── catalogue_contents_inspector.py
│   ├── catalogue_contents_region_selector.py
│   ├── CATALOGUE_CONTENTS_EXTRACTOR_GUIDE.md
│   ├── example_config_gewiss_contents.yaml
│   ├── example_config_ec_contents.yaml
│   ├── example_config_robus_contents.yaml
│   └── requirements.txt
│
└── .gitignore
```

---

## The two extraction packs

### 1. Catalogue Index Extractor

Use this when the catalogue has product-code index pages, usually near the back of the catalogue.

Example input layout:

```text
Code          Page
GW 21 005     436
RTW0330C-24   171
RVC4K675      212
```

Typical output:

```csv
sku,page
GW 21 005,436
RTW0330C-24,171
RVC4K675,212
```

The index extractor can also capture optional row-level fields such as:

* Pack/carton
* Pallet
* Trade price
* Other catalogue-specific index columns

It supports three extraction modes:

| Mode                   |      Config value | Use when                                                             |
| ---------------------- | ----------------: | -------------------------------------------------------------------- |
| Automatic header mode  |            `auto` | The index page has reliable column headers such as `Code` and `Page` |
| Manual coordinate mode |          `manual` | You know the exact PDF x/y column boundaries                         |
| Visual template mode   | `visual_template` | You want to draw boxes around columns using a PDF preview            |

---

### 2. Catalogue Contents Extractor

Use this when the catalogue has contents pages that describe the catalogue structure.

Example input layout:

```text
Section 2 Cable Accessories
  Fixings & Fastenings
    Cable Ties ................................ 74
```

Typical output:

```csv
main_header,subheader_1,subheader_2,page_number
Section 2 Cable Accessories,Fixings & Fastenings,Cable Ties,74
```

The contents extractor supports multiple catalogue styles, including:

| Mode           | Use when                                                                           |
| -------------- | ---------------------------------------------------------------------------------- |
| `style_rules`  | Headings and item rows can be separated by font, size, colour, position, or regex  |
| `region_rules` | The page has multiple columns, regions, or areas that need to be included/excluded |
| `card_grid`    | Contents are shown as visual cards/tiles with page numbers and titles              |
| `area_table`   | The safest method is to define separate x/y regions for each output column         |

---

## Requirements

The tools are written in Python and use common PDF/data-processing libraries.

Main dependencies include:

* `PyMuPDF`
* `pdfplumber`
* `pandas`
* `openpyxl`
* `PyYAML`
* `Pillow`

Python 3.10+ is recommended.

---

## Installation

Clone the repository:

```bash
git clone https://github.com/maxautomateseverything/Product-Catalogue-Extraction.git
cd Product-Catalogue-Extraction
```

Create and activate a virtual environment.

### Windows PowerShell

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
```

### macOS / Linux

```bash
python3 -m venv .venv
source .venv/bin/activate
```

Install requirements for the pack you want to use.

### Catalogue Index Extractor

```bash
cd catalogue_index_extractor_pack
python -m pip install -r requirements.txt
```

On Windows, you can also use:

```powershell
cd catalogue_index_extractor_pack
py -m pip install -r requirements.txt
```

### Catalogue Contents Extractor

```bash
cd catalogue_contents_extractor_pack
python -m pip install -r requirements.txt
```

On Windows:

```powershell
cd catalogue_contents_extractor_pack
py -m pip install -r requirements.txt
```

---

## Quick start: extract a product-code index

Go to the index extractor folder:

```bash
cd catalogue_index_extractor_pack
```

Copy an example config and edit it for your catalogue:

```bash
cp example_config_gewiss_style.yaml my_index_config.yaml
```

On Windows PowerShell:

```powershell
Copy-Item example_config_gewiss_style.yaml my_index_config.yaml
```

Edit the config file and set:

```yaml
input_pdf: "C:/Path/To/catalogue.pdf"
output_folder: "C:/Path/To/Index Output"
index_pdf_pages: "1293-1364"
page_source_of_truth: "pdf_page_number"
```

Run the extractor:

```bash
python catalogue_index_extractor.py --config my_index_config.yaml
```

On Windows:

```powershell
py catalogue_index_extractor.py --config my_index_config.yaml
```

You can also override the input, output, or pages from the command line:

```bash
python catalogue_index_extractor.py ^
  --config my_index_config.yaml ^
  --input "C:/Path/To/catalogue.pdf" ^
  --output "C:/Path/To/Index Output" ^
  --pages "1293-1364"
```

---

## Recommended index-extraction workflow

For a new catalogue, use this process:

1. **Identify the index pages** in the PDF.
2. **Copy an example config** and update the PDF path, page range, and output folder.
3. **Run a layout scan** if the index has repeated table layouts.
4. **Use visual template mode** if automatic column detection is not reliable.
5. **Run the extractor**.
6. **Open the review workbook** and inspect warnings.
7. **Adjust the config/template and rerun** until the output is clean.

### Scan index layouts

```bash
python catalogue_index_extractor.py --config my_index_config.yaml --scan-layouts
```

This creates layout review files such as:

```text
layout_scan_review.csv
layout_scan_summary.csv
layout_scan_summary.json
```

Use the scan result to decide whether one visual template is enough or whether different page groups need different templates.

### Create a visual template

```bash
python catalogue_region_selector.py \
  --pdf "C:/Path/To/catalogue.pdf" \
  --config "my_index_config.yaml" \
  --page 1312 \
  --zoom 2.0 \
  --output-template "layout_1_template.json" \
  --output-config "my_index_config_with_visual.yaml" \
  --apply-pages "1293-1364"
```

In the visual selector, draw boxes around each column you want to extract, such as:

```text
block_1 sku
block_1 page
block_2 sku
block_2 page
block_3 sku
block_3 page
```

Then run extraction with the visual template:

```bash
python catalogue_index_extractor.py \
  --config my_index_config.yaml \
  --visual-template "layout_1_template.json" \
  --visual-template-pages "1293-1364"
```

---

## Quick start: extract catalogue contents pages

Go to the contents extractor folder:

```bash
cd catalogue_contents_extractor_pack
```

Copy one of the example configs:

```bash
cp example_config_gewiss_contents.yaml my_contents_config.yaml
```

On Windows PowerShell:

```powershell
Copy-Item example_config_gewiss_contents.yaml my_contents_config.yaml
```

Edit the config file:

```yaml
input_pdf: "C:/Path/To/catalogue.pdf"
output_folder: "C:/Path/To/Contents Output"
contents_pdf_pages: "13-14"

extraction:
  mode: "region_rules"

output_columns:
  - main_header
  - subheader_1
  - subheader_2
  - subheader_3
  - page_number
```

Run the extractor:

```bash
python catalogue_contents_extractor.py --config my_contents_config.yaml
```

On Windows:

```powershell
py catalogue_contents_extractor.py --config my_contents_config.yaml
```

You can also override config values from the command line:

```bash
python catalogue_contents_extractor.py \
  --config my_contents_config.yaml \
  --input "C:/Path/To/catalogue.pdf" \
  --pages "13-14" \
  --output "C:/Path/To/Contents Output"
```

---

## Recommended contents-extraction workflow

For a new catalogue, use this process:

1. **Identify the contents pages** in the PDF.
2. **Run the inspector** on representative pages.
3. **Open the inspection workbook** and review fonts, colours, coordinates, and line records.
4. **Choose the extraction mode** that best fits the catalogue.
5. **Edit a YAML config** using rules, regions, or card/grid settings.
6. **Use the region selector** if you need to draw page regions visually.
7. **Run the extractor**.
8. **Review the CSV and workbook outputs**.
9. **Adjust the config and rerun** until validation examples pass.

### Run the contents inspector

The inspector should usually be run before writing detailed extraction rules:

```bash
python catalogue_contents_inspector.py \
  --input "C:/Path/To/catalogue.pdf" \
  --pages "13-14" \
  --output "C:/Path/To/Inspection Output"
```

Or run it from a config:

```bash
python catalogue_contents_inspector.py --config my_contents_config.yaml
```

The inspector creates:

```text
contents_inspection_workbook.xlsx
inspection_debug_images/
```

Use the workbook to understand the PDF’s actual text layer, including:

* Text lines
* Coordinates
* Font names
* Font sizes
* Colours
* Suggested extraction rules

---

## Output files

### Index extractor outputs

The index extractor produces a clean SKU/page registry and review evidence.

Common outputs include:

```text
sku_registry.csv
extraction_review_workbook.xlsx
debug_images/
layout_scan_review.csv
layout_scan_summary.csv
layout_scan_summary.json
```

The most important file is:

```text
sku_registry.csv
```

This contains the clean product-code-to-page registry.

The review workbook should be checked for:

* Missing page values
* Unexpected optional-column values
* Pages with fewer table blocks than expected
* Rows that need manual review
* Extraction warnings

---

### Contents extractor outputs

The contents extractor writes outputs to the configured `output_folder`:

```text
contents.registry.csv
contents_rows.csv
contents_review_workbook.xlsx
contents_config_used.yaml
debug_images/
```

#### `contents.registry.csv`

The clean main output. It contains only the configured output columns, for example:

```csv
main_header,subheader_1,subheader_2,subheader_3,page_number
```

#### `contents_rows.csv`

A fuller debug/review output containing source fields such as:

```text
source_pdf_page
source_region
source_line_ids
raw_text
classification_rule
confidence_status
review_reason
row_type
```

Use this file when checking exactly how a row was created.

#### `contents_review_workbook.xlsx`

The main review workbook. It typically includes sheets such as:

```text
Run Summary
Page Diagnostics
Extracted Contents Review
Unresolved Rows
Line Classification
Rule Matches
Validation Examples
Ignored Lines
Extractor Errors
```

#### `debug_images/`

Debug images show configured regions, selected areas, card zones, issue pages, and other visual extraction evidence.

---

## Configuration overview

Both packs are config-driven. The recommended approach is to copy an example YAML file and edit the copy.

Common config fields include:

```yaml
input_pdf: "C:/Path/To/catalogue.pdf"
output_folder: "C:/Path/To/Output"
```

For the index extractor:

```yaml
index_pdf_pages: "1293-1364"
page_source_of_truth: "pdf_page_number"
```

For the contents extractor:

```yaml
contents_pdf_pages: "13-14"

extraction:
  mode: "region_rules"
```

Page ranges support formats such as:

```yaml
contents_pdf_pages: "13-14"
contents_pdf_pages: "4,6,8"
contents_pdf_pages: "4-6,9-10"
contents_pdf_pages: "all"
```

---

## Validation and review philosophy

The tools are designed to be semi-guided rather than fully automatic.

Catalogue layouts vary heavily between suppliers, so the safest workflow is:

1. Configure extraction rules or regions.
2. Run the extractor.
3. Review the evidence workbook.
4. Fix the config.
5. Rerun until the output is trusted.

The project’s practical safety principle is:

> Do not silently lose useful catalogue data.

Rows that are uncertain should be retained, flagged, and reviewed rather than silently discarded.

---

## Common troubleshooting

### The output is empty

Check that:

* The PDF path is correct
* The page range is correct
* The PDF has selectable text
* The configured extraction mode matches the catalogue layout
* Regions are not excluding the useful text
* The config file is being loaded correctly

### The wrong text is being extracted

Check:

* Region coordinates
* Ignore regions
* Header detection rules
* Font-size and colour filters
* Whether the page has multiple layout types
* Whether the visual template was applied to the correct pages

### Page numbers are missing

Check:

* The page-number regex
* Whether page numbers are leading, trailing, or in a separate column
* Whether page numbers are inside the selected region
* Whether page ranges are printed explicitly or need to be derived

### Multi-line product names are split

For contents pages, enable or adjust:

```yaml
multiline_items:
  enabled: true
  same_style_required: true
  max_vertical_gap: 8
```

### The visual selector opens, but extraction is still wrong

Remember that the visual selector only renders a preview image so you can draw boxes. Extraction still uses the original PDF text layer. Do not convert the PDF into image-only pages before extraction.

---

## Best practices

* Always work on a copied config file, not the original example.
* Start with a small page range before processing the full catalogue.
* Use the inspector or layout scan before writing complex rules.
* Keep review workbooks from each run while developing a config.
* Use visual templates for layouts with shifted columns or unreliable headers.
* Add validation examples for important rows that must be found.
* Treat `needs_review` rows as part of the workflow, not as a failure.
* Keep supplier-specific configs named clearly, for example:

```text
config_supplier_name_index.yaml
config_supplier_name_contents.yaml
```

---

## Suggested workflow for a full catalogue

A complete catalogue-processing workflow might look like this:

1. Extract the contents pages using `catalogue_contents_extractor_pack`.
2. Review `contents.registry.csv`.
3. Extract the product-code index using `catalogue_index_extractor_pack`.
4. Review `sku_registry.csv`.
5. Use the contents registry and SKU registry as inputs for downstream product-table extraction or catalogue enrichment.

---

## Documentation

Detailed pack-specific guides are included inside each folder:

```text
catalogue_index_extractor_pack/CATALOGUE_INDEX_EXTRACTOR_GUIDE.md
catalogue_index_extractor_pack/CATALOGUE_INDEX_EXTRACTOR_USER_GUIDE_NO_CODE.md
catalogue_contents_extractor_pack/CATALOGUE_CONTENTS_EXTRACTOR_GUIDE.md
```

Use the repo-level README for orientation, then refer to the pack-specific guides when tuning configs for a particular supplier catalogue.

---

## Limitations

This project currently assumes:

* The PDF has selectable text
* The relevant catalogue pages are known or manually defined
* The user can review outputs and adjust configs
* Scanned/image-only PDFs are not handled reliably without OCR
* Different suppliers may require different YAML configs
* Some layouts require manually drawn visual regions/templates

---

## License

No license file is currently included in this repository. Add a `LICENSE` file before distributing or reusing the code outside your own environment.

---

## Project status

This repository is an active catalogue-extraction toolkit with supplier-specific example configs and detailed user guides. It is best used as a practical, config-driven extraction framework rather than a one-click universal PDF parser.
