# Catalogue Table Extractor — Final Table-Focused Pack

This pack extracts **tables and table-like product matrices from selectable-text PDF catalogues**. It is designed for catalogues in which:

- Table columns change from page to page.
- A product code can be on the left, right, inside the matrix, repeated several times in one row, or used as a column heading.
- Cells visually span several rows.
- Printed catalogue page numbers differ from PDF viewer page numbers.
- A pre-existing SKU registry and SKU index are available.
- The SKU index points to likely pages but does not describe every page on which a SKU can occur.
- A full run may involve 1,000 or more pages and must be resumable.
- Local AI is useful for exceptions, but too slow to run indiscriminately against every table.

This package is deliberately **table-focused**. Feature bullets, construction text, icon specifications, prose, diagrams and other non-table product information belong in a separate non-table extractor.

---

# Part I — How the code works

This section explains the current logic in the order in which the program runs.

## 1. Inputs and their roles

The extractor can accept four main inputs.

### Catalogue PDF

The complete selectable-text catalogue.

Example:

```text
GEWISS Trade Catalogue 2025-26.pdf
```

### Canonical SKU registry

Supplied with:

```text
--sku-registry
```

This should contain one row per unique SKU. It is treated as the canonical identity list.

The uploaded GEWISS registry includes fields such as:

- SKU and normalized SKU
- Confidence status
- Printed catalogue pages
- Source PDF index pages
- Pack/carton values
- Pallet values
- Required issues and optional warnings
- Occurrence count

Rows marked `needs_review` are **included**, not discarded. Their review status is carried into the output.

### SKU index rows

Supplied with:

```text
--sku-index-rows
```

This is occurrence-level index provenance. One SKU can have several index rows.

The important page fields have different meanings:

- `source_pdf_page` is the actual PDF page on which the index row was found.
- `catalogue_page_normalized` is the printed product page to which the index points.
- These values are not interchangeable.
- A page-number offset is required to convert the printed page into the full PDF's page position.

An index pointer is treated as **guidance**, not as a hard boundary. A SKU indexed to printed page 15 might also have relevant information on printed pages 14 and 16, or elsewhere in the catalogue.

### Optional previous code list

A simpler historical code list can be supplied with:

```text
--product-code-index
```

This is useful when no full registry exists. When both files exist, the canonical SKU registry and index rows are preferred.

---

## 2. Registry loading and normalization

The program loads the canonical SKU registry and occurrence-level index separately, then combines their metadata by normalized SKU.

For matching purposes, a normalized form is created by:

- Converting to uppercase.
- Removing spaces and selected punctuation.
- Preserving the original displayed SKU separately.

Example:

```text
GW 60 082 → GW60082
```

Matching priorities are:

1. Exact displayed value.
2. Exact normalized value.
3. Controlled fuzzy candidate for review only.

Fuzzy matching never silently creates a confirmed product match.

The combined registry preserves:

- Canonical status
- `confirmed` or `needs_review`
- Printed page pointers
- Index source pages
- Pack/carton raw and parsed values
- Pallet raw and parsed values
- Registry issues and warnings

---

## 3. Printed-page to PDF-page mapping

Catalogues frequently include covers and introductory pages, so printed page 64 is not necessarily PDF page 64.

The code inspects standalone page-number text near page edges and estimates the dominant relationship:

```text
PDF page = printed catalogue page + offset
```

During analysis of the two supplied full catalogues, representative dominant offsets were:

```text
GEWISS: PDF page = printed page + 1
ROBUS:  PDF page = printed page + 2
```

The result is written to:

```text
catalogue_page_mapping.json
```

The automatic result can be overridden:

```powershell
--catalogue-page-offset 1
```

or left automatic:

```powershell
--catalogue-page-offset auto
```

---

## 4. Nearby-page windows

For every printed page listed in the SKU index, the program calculates:

- The mapped core PDF page.
- A configurable page window before and after it.

The default is:

```text
--index-page-radius 1
```

Therefore, an SKU mapped to PDF page 16 is expected on:

```text
15, 16 or 17
```

This window is used for:

- Targeted page selection with `--pages registry`.
- Coverage reporting.
- Page-relationship labels.
- Review prioritisation.

It is not used to reject exact matches outside the window. An exact match elsewhere is retained and labelled as an outside-window occurrence.

The page plan is written to:

```text
sku_page_plan.csv
```

---

## 5. Page selection

The extractor supports:

```text
--pages all
--pages 1-10
--pages 33,65,80
--pages 100-
--pages registry
```

Page values are PDF viewer positions and are 1-based.

`--pages registry` processes the union of the mapped SKU index windows.

A prior review queue can override ordinary page selection:

```text
--review-queue-input review_queue.csv
```

Only pages with unresolved review rows are selected.

---

## 6. Fast native-text SKU discovery

Before the expensive table-layout stage, the program can scan selectable PDF text to find likely product codes.

The scope is controlled by:

```text
--code-registry-scope all
--code-registry-scope selected
--code-registry-scope off
```

The discovered code list is merged with the supplied canonical registry.

This creates:

```text
product_code_registry.csv
product_code_registry.json
```

The PDF scan is not authoritative by itself. It is used to improve recall and identify unexpected codes.

---

## 7. Large-catalogue batching

PyMuPDF4LLM layout analysis can use significant memory. The program therefore processes the selected catalogue pages in batches.

Default:

```text
--layout-batch-size 10
```

For every batch, the program:

1. Creates a temporary PDF containing only those source pages.
2. Runs PyMuPDF4LLM against the small subset.
3. Writes a batch JSON checkpoint.
4. Processes each detected table.
5. Deletes the temporary PDF after success unless told not to.
6. Releases large in-memory registry and layout objects between batches.

Batch files are stored under:

```text
layout_batches/
```

and summarised in:

```text
pymupdf4llm_layout_manifest.json
```

This isolates failures and allows a long run to continue from prior checkpoints.

---

## 8. Primary table detection with PyMuPDF4LLM

The primary detector is:

```python
pymupdf4llm.to_json(...)
```

with layout analysis enabled.

Only layout boxes whose class is:

```text
table
```

are treated as candidate tables.

For every candidate, the program keeps:

- The table bounding box.
- Row and column counts.
- Extracted cell matrix.
- Cell coordinates.
- Original layout JSON.
- Natural table-specific columns.

The extractor does not require one global schema. A dimensions table, electrical ratings table and compatibility matrix can all retain different columns.

---

## 9. Sparse edge-column refinement

A catalogue may place a product image or caption immediately beside a table. A layout detector can mistakenly include it as a sparse first or last column.

The deterministic refinement stage measures:

- Number of non-empty cells.
- Fill ratio.
- Header content.
- Duplicate caption behaviour.
- Edge position.

Only sparse **outer** columns are eligible for automatic removal. Internal columns are not deleted by this rule.

Diagnostics include:

```text
page_overlay.png
```

where the original and refined boundaries can be compared.

Manual overrides remain available for exceptional pages.

---

## 10. Companion PyMuPDF line-grid extraction

PyMuPDF4LLM is strong at finding layout tables, including borderless ones, but merged-cell relationships can be easier to recover from ruled grid geometry.

For each candidate, the program also tries:

```python
page.find_tables(...)
```

The line-grid result is accepted only when it is compatible with the PyMuPDF4LLM candidate by:

- Bounding-box overlap.
- Row count.
- Column count.
- Overall compatibility score.

This prevents an unrelated grid from replacing the layout result.

When accepted, the grid contributes:

- Physical row and column boundaries.
- `None` placeholders for covered rowspan or colspan positions.
- Native selectable text clipped from physical cells.

---

## 11. True merged-cell expansion

A CSV cannot represent a vertical merged cell. The extractor therefore repeats a genuinely spanning value across the rows it visually covers.

Example visual table:

```text
Without cable | 16 A | 100–130 V | 2P+E | GW 64 008 | GW 64 022
              |      |           | 3P+E |           | GW 64 023
```

Normalized matrix:

```text
Without cable | 16 A | 100–130 V | 2P+E | GW 64 008 | GW 64 022
Without cable | 16 A | 100–130 V | 3P+E |           | GW 64 023
```

The program distinguishes:

- A true cell covered by a merged cell: repeat the source value.
- A genuine empty cell: leave it empty.

Every duplication is recorded in:

```text
merged_cell_expansion.json
```

This makes the transformation auditable.

---

## 12. Borderless word-column repair

Some borderless ROBUS dimensions tables contain four visual columns, but PyMuPDF4LLM may combine the last two.

Example source:

```text
SKU | Length | Height/Depth | Width
```

Incorrect extraction:

```text
SKU | Length | Height/Depth Width
```

The repair stage examines native word x-coordinates across repeated data rows.

A new column boundary is accepted only when:

- A larger column count is supported by enough rows.
- The x-position clusters are stable.
- The repair is consistent with the table bounding box.
- It improves the data-row structure.

For the ROBUS page 80 example, this recovers:

```csv
DIMENSIONS (mm),Length,Height/Depth,Width
RVA3K1,1000,2,8
RVA4K1,1000,2,8
...
```

The full ROBUS catalogue contains many `DIMENSIONS (mm)` and `PRODUCT` blocks, including matrices where SKUs are row labels and others where SKUs are column headings. fileciteturn3file0 The focused page-80 sample shows the six RVA codes and separate Length, Height/Depth and Width fields that motivated this repair. fileciteturn3file10

---

## 13. Header reconstruction and section context

The deterministic formatter:

- Flattens initial multi-row headers.
- Preserves source header wording.
- Normalizes line breaks and whitespace.
- Removes section-only rows from product records.
- Carries section context into subsequent rows.

Examples of section context:

```text
Rated current (A): 16
Rated current (A): 32
Without cable
With cable
```

The original source header is never discarded.

For each attribute, the program stores:

```text
source_attribute
normalized_attribute
attribute_value
```

Example:

```text
Rated current (In) → rated_current
Height/Depth       → height_depth
```

Unknown headers receive a stable normalized snake-case name while preserving the original.

---

## 14. Dynamic SKU location logic

The program does not assume that the first column is always the SKU.

It supports:

### Left row index

```text
RVA3K1 | 1000 | 2 | 8
```

### Right row index

```text
1000 | 2 | 8 | RVA3K1
```

### Internal matrix cell

```text
Without cable | 16 A | GW 64 008 | GW 64 022
```

### Several SKUs in one row

One product occurrence is created for every SKU cell, with shared row values repeated.

### SKU column headings

Example:

```text
Attribute     | SKU-A | SKU-B
Total Power   | 10 W  | 20 W
Weight        | 1 kg  | 2 kg
```

The program transposes the matrix into one product occurrence per SKU.

### Several SKUs in one cell

Every registry-recognized code is separated and recorded when the source cell contains multiple codes.

Every occurrence records:

```text
code_position
source_rows
source_columns
pdf_page
table
```

---

## 15. Option-code and false-positive protection

Short option values such as:

```text
E
STE
SEN
DD
AD
```

must not automatically become products.

The detector gives priority to canonical registry matches. Unknown values are accepted as SKU candidates only when their shape and table context are strongly product-code-like.

Values resembling:

- Quantities
- Current values
- Voltage values
- Multipliers such as `1x16A`
- Pack/carton values
- Common option abbreviations

are excluded from automatic product creation unless they are present in the canonical registry.

---

## 16. One occurrence per product code

When a visual row contains several product codes, the program creates one occurrence per code.

Shared values are repeated, and the code column's header is preserved as:

```text
output_configuration
```

For example:

```text
GW 64 008
product_type = Without cable
rated_current = 16 A
rated_voltage = 100 ÷ 130 V
poles = 2P+E
output_configuration = No. 2 socket-outlets 16A
```

and:

```text
GW 64 022
product_type = Without cable
rated_current = 16 A
rated_voltage = 100 ÷ 130 V
poles = 2P+E
output_configuration = No. 3 socket-outlets 16A
```

The GEWISS catalogue contains dense code tables, output matrices and repeated product mini-tables, so retaining the natural table schema and column meaning is essential. fileciteturn3file1

---

## 17. Three-level product data model

The final catalogue outputs separate identity, occurrence and attribute data.

### `products.csv`

One row per unique SKU.

Important fields include:

```text
catalogue_id
manufacturer
sku
sku_normalized
registry_status
extraction_status
occurrence_count
catalogue_pages_normalized
expected_pdf_page_core
expected_pdf_pages_with_radius
found_pdf_pages
pack_carton_raw
pack_carton
pallet_raw
pallet
conflict_count
needs_review
attributes_json
```

### `product_occurrences.csv`

One row for every SKU location.

```text
occurrence_id
sku
pdf_page
table
code_position
source_rows
source_columns
page_relationship
method
```

This preserves all duplicate occurrences.

### `product_attributes.csv`

One row per occurrence attribute.

```text
occurrence_id
sku
source_attribute
normalized_attribute
attribute_value
pdf_page
table
source_row
source_column
inherited
page_relationship
method
```

This long format is the recommended master representation for tables with changing schemas.

---

## 18. Duplicate and conflicting information

All occurrences are preserved.

A consolidated product record is also built from all occurrences.

When the same normalized attribute has different values:

- Both source values remain in `product_attributes.csv`.
- The product is not silently overwritten.
- The conflict is written to:

```text
attribute_conflicts.csv
```

- A corresponding row is added to:

```text
review_queue.csv
```

This supports the agreed option of preserving all occurrences while creating one consolidated product record.

---

## 19. Registry coverage and unexpected codes

The extractor produces both sides of the coverage comparison.

### `unmatched_registry_skus.csv`

Canonical SKUs that were expected but not extracted from the processed scope.

### `unexpected_pdf_skus.csv`

Strong PDF SKU candidates that were not present in the canonical registry.

Unexpected codes are not silently confirmed. They are included as unconfirmed review items.

### `fuzzy_match_candidates.csv`

Possible non-exact matches for human review.

### `product_code_coverage.csv`

Registry-versus-extraction coverage summary.

An exact SKU match outside the index page window is preserved and marked with a page relationship such as:

```text
outside_index_window
```

---

## 20. Pack/carton and pallet enrichment

Pack/carton and pallet information from the canonical registry is added to the consolidated product record even if it is not repeated on the product page.

Both forms are retained:

```text
pack_carton_raw
pack_carton
pallet_raw
pallet
```

This avoids losing formatting or interpretation details from the original index extraction.

---

## 21. Optional AI exception routing

Ollama is not required for deterministic extraction.

The recommended large-catalogue strategy is:

1. Deterministic full run.
2. Coverage and review analysis.
3. AI only for unresolved or suspicious pages.
4. Human review of remaining conflicts.
5. Resumable rebuild of catalogue-wide outputs.

Default AI routing:

```text
--ai-review-policy auto
--ai-structure-input auto
```

The model may be called for:

- Suspicious table boundaries.
- Unresolved split or merged columns.
- Visual-only fields such as colour swatches.
- Unknown header normalization.
- Low registry coverage.
- Conflicting values selected for review.

Reliable grid tables can skip the expensive boundary-review call.

Most structure calls can use the compact text matrix instead of full-page vision.

### Recommended models

For a CPU-only computer:

```text
qwen3-vl:4b-instruct
```

is the recommended routine exception model.

Use:

```text
qwen3-vl:8b-instruct
```

only for selected difficult pages when the 4B output is insufficient.

The default safe policy is:

```text
--ai-low-confidence-action keep-deterministic
```

Low-confidence AI output is saved for inspection but does not replace the deterministic result.

---

## 22. Review queue

The editable review file contains:

```text
source_pdf
page
table
sku
issue_type
deterministic_result
ai_suggestion
confidence
source_crop_path
review_status
reviewer_value
attribute_name
```

Common issue types include:

- Attribute conflict
- Unexpected PDF SKU
- Unmatched registry SKU
- Suspicious boundary
- Outside index window
- Low AI confidence
- Unresolved visual cell

Suggested review statuses:

```text
open
approved
corrected
ignore
resolved
```

To correct a value:

1. Enter the corrected value in `reviewer_value`.
2. Set `review_status` to `corrected`.
3. Save the CSV.
4. Run an exception pass using `--review-queue-input`.

---

## 23. Resume and restart logic

The extractor writes checkpoints at two levels.

### Layout batch checkpoint

```text
layout_batches/batch_XXXXX_pages_XXXX-XXXX.json
```

With `--resume`, completed layout batches are reused.

### Per-table checkpoint

```text
page_XXXX/table_YY/final.json
```

With `--resume`, completed tables are loaded without repeating extraction or AI.

### Interrupted full run

Run the same command again with:

```text
--resume
```

### Exception rerun

Use the same output folder, but process the review queue **without** resume:

```text
--review-queue-input review_queue.csv
```

This reprocesses the selected pages and replaces their per-table outputs.

### Rebuild complete aggregates after an exception pass

Run the original full command again with:

```text
--pages all
--resume
```

Cached layout batches and table `final.json` files are reused, and the top-level product and attribute CSVs are rebuilt across the complete catalogue.

The PowerShell launcher can perform this rebuild automatically after an AI exception pass.

---

# Part II — How to use the pack

The following steps are written for a Windows user with little command-line experience.

## 24. Folder contents

After extracting the ZIP, keep these files together:

```text
CatalogueTableExtractor/
├── catalogue_table_extractor.py
├── run_extractor.ps1
├── config.example.json
├── README.md
├── RELEASE_NOTES.md
├── CATALOGUE_INSPECTION_REPORT.md
└── catalogue_inspection_points.csv
```

Your PDF and registry files can be stored elsewhere.

---

## 25. Install Python

Install Python 3.11 or 3.12 from the official Python website.

During installation, select:

```text
Add python.exe to PATH
```

Open PowerShell and check:

```powershell
py --version
```

A successful result looks like:

```text
Python 3.12.x
```

---

## 26. Install Python packages

Open the extracted pack folder in File Explorer.

Click the address bar, type:

```text
powershell
```

and press Enter.

Install the packages:

```powershell
py -m pip install --upgrade pymupdf pymupdf4llm pillow
```

Check the script:

```powershell
py ".\catalogue_table_extractor.py" --help
```

---

## 27. Optional: install Ollama

Ollama is only needed for AI exception processing.

Install Ollama, then check:

```powershell
ollama --version
```

Recommended routine model:

```powershell
ollama pull qwen3-vl:4b-instruct
```

Optional difficult-page model:

```powershell
ollama pull qwen3-vl:8b-instruct
```

Check:

```powershell
ollama list
```

---

## 28. Easiest method: guided PowerShell launcher

Right-click `run_extractor.ps1` and choose **Run with PowerShell**.

If Windows blocks the script, open PowerShell in the pack folder and run:

```powershell
powershell -ExecutionPolicy Bypass -File ".\run_extractor.ps1"
```

The launcher asks you to select:

- Catalogue PDF
- Canonical SKU registry
- SKU index rows
- Output folder
- Manufacturer
- Catalogue ID
- Run mode
- Optional Ollama model

It writes the chosen settings to:

```text
last_run_config.json
```

That file can be reused later.

### Launcher run modes

1. **Small deterministic test**  
   Select a few PDF pages.

2. **Full deterministic catalogue run**  
   Recommended first production pass.

3. **Registry-targeted deterministic run**  
   Processes mapped index pages plus nearby-page windows.

4. **AI exception pass**  
   Uses an existing `review_queue.csv`, then offers to rebuild complete aggregates.

5. **Full AI automatic run**  
   Available, but not recommended as the first run on a CPU-only computer.

6. **Resume/rebuild**  
   Reuses existing checkpoints.

---

## 29. Recommended first GEWISS test

Use the inspection pages:

```text
33,65,708,1293
```

PowerShell command:

```powershell
py ".\catalogue_table_extractor.py" `
  "C:\Catalogues\gewiss-trade-catalogue-2025---2026-en.pdf" `
  --catalogue-id "GEWISS-2025-26" `
  --manufacturer "GEWISS" `
  --pages "33,65,708,1293" `
  --output "C:\Catalogue Results\GEWISS Test" `
  --sku-registry "C:\Registries\GEWISS\sku_registry.csv" `
  --sku-index-rows "C:\Registries\GEWISS\index_rows.csv" `
  --catalogue-page-offset "auto" `
  --index-page-radius 1
```

Inspect:

```text
catalogue_page_mapping.json
sku_page_plan.csv
page_0033/
page_0065/
products.csv
product_attributes.csv
product_occurrences.csv
review_queue.csv
```

---

## 30. Recommended first ROBUS test

Use:

```text
80,161,165,448
```

```powershell
py ".\catalogue_table_extractor.py" `
  "C:\Catalogues\ROBUS UK & IRE 2026 CATALOGUE.pdf" `
  --catalogue-id "ROBUS-2026" `
  --manufacturer "ROBUS" `
  --pages "80,161,165,448" `
  --output "C:\Catalogue Results\ROBUS Test" `
  --sku-registry "C:\Registries\ROBUS\sku_registry.csv" `
  --sku-index-rows "C:\Registries\ROBUS\index_rows.csv" `
  --catalogue-page-offset "auto" `
  --index-page-radius 1
```

Page 80 tests borderless dimensions. Page 161 tests option-code exclusion. Page 165 tests SKUs used as matrix columns. Page 448 tests several product blocks on one page.

---

## 31. Full deterministic production run

This should be the first complete pass.

```powershell
py ".\catalogue_table_extractor.py" `
  "C:\Catalogues\Full Catalogue.pdf" `
  --catalogue-id "CATALOGUE-ID" `
  --manufacturer "MANUFACTURER" `
  --pages "all" `
  --output "C:\Catalogue Results\Catalogue Deterministic" `
  --sku-registry "C:\Registries\sku_registry.csv" `
  --sku-index-rows "C:\Registries\index_rows.csv" `
  --catalogue-page-offset "auto" `
  --index-page-radius 1 `
  --layout-batch-size 10 `
  --code-registry-scope "all" `
  --resume
```

Do not add an Ollama model.

The run can take a long time but is checkpointed.

---

## 32. Registry-targeted run

Use this when you first want to inspect the pages indicated by the SKU index and their neighbours:

```powershell
py ".\catalogue_table_extractor.py" `
  "C:\Catalogues\Full Catalogue.pdf" `
  --pages "registry" `
  --output "C:\Catalogue Results\Registry Targeted" `
  --sku-registry "C:\Registries\sku_registry.csv" `
  --sku-index-rows "C:\Registries\index_rows.csv" `
  --index-page-radius 1 `
  --resume
```

This is faster than all pages, but it can miss unindexed product-information pages. The complete deterministic run remains the coverage baseline.

---

## 33. Using a JSON configuration

Copy:

```text
config.example.json
```

to a new file, for example:

```text
gewiss.production.json
```

Edit the paths in Notepad.

Run:

```powershell
py ".\catalogue_table_extractor.py" --config ".\gewiss.production.json"
```

Command-line values override the JSON settings:

```powershell
py ".\catalogue_table_extractor.py" `
  --config ".\gewiss.production.json" `
  --pages "33,65"
```

---

## 34. Review the first full run

Open these top-level files in this order.

### `manifest.json`

Confirms:

- Selected pages
- Number of tables
- Number of product records
- Registry size
- Unmatched and unexpected counts
- Batch and AI settings

### `products.csv`

One row per consolidated SKU.

### `product_occurrences.csv`

All SKU locations.

### `product_attributes.csv`

All extracted attributes with source and normalized headers.

### `product_code_coverage.csv`

Coverage against the canonical registry.

### `unmatched_registry_skus.csv`

Known SKUs not extracted.

### `unexpected_pdf_skus.csv`

Possible new or misread codes.

### `attribute_conflicts.csv`

Different values associated with the same SKU and normalized attribute.

### `review_queue.csv`

Unified action list.

---

## 35. Review individual tables

Each table directory contains:

```text
page_XXXX/
└── table_YY/
    ├── layout_matrix.csv
    ├── grid_matrix.csv
    ├── raw_matrix.csv
    ├── refined_matrix.csv
    ├── normalized_matrix.csv
    ├── deterministic_structured.csv
    ├── deterministic_product_records.csv
    ├── merged_cell_expansion.json
    ├── geometry.json
    ├── page_overlay.png
    ├── refined_crop.png
    ├── quality_report.json
    ├── final.csv
    ├── product_records.csv
    └── final.json
```

Check in this order:

1. `page_overlay.png`
2. `normalized_matrix.csv`
3. `deterministic_product_records.csv`
4. `quality_report.json`
5. `final.csv`

---

## 36. AI exception pass

After editing `review_queue.csv`, process only unresolved pages:

```powershell
py ".\catalogue_table_extractor.py" `
  "C:\Catalogues\Full Catalogue.pdf" `
  --output "C:\Catalogue Results\Catalogue Deterministic" `
  --sku-registry "C:\Registries\sku_registry.csv" `
  --sku-index-rows "C:\Registries\index_rows.csv" `
  --review-queue-input "C:\Catalogue Results\Catalogue Deterministic\review_queue.csv" `
  --ollama-model "qwen3-vl:4b-instruct" `
  --ai-mode "structure" `
  --ai-review-policy "auto" `
  --ai-structure-input "auto" `
  --ollama-num-ctx 16384 `
  --ollama-timeout 1800 `
  --ollama-keep-alive "60m" `
  --ai-low-confidence-action "keep-deterministic"
```

Do not add `--resume` to the exception pass, because selected table outputs need to be regenerated.

---

## 37. Rebuild complete catalogue outputs

After the exception pass:

```powershell
py ".\catalogue_table_extractor.py" `
  "C:\Catalogues\Full Catalogue.pdf" `
  --pages "all" `
  --output "C:\Catalogue Results\Catalogue Deterministic" `
  --sku-registry "C:\Registries\sku_registry.csv" `
  --sku-index-rows "C:\Registries\index_rows.csv" `
  --resume
```

The program reuses the cached layout batches and per-table `final.json` files, then rebuilds the combined CSVs.

---

## 38. Restart after interruption

Run the same original command with:

```text
--resume
```

Do not delete:

```text
layout_batches/
page_XXXX/table_YY/final.json
manifest.json
```

The logs identify reused batches and tables.

---

## 39. Output-folder guidance

Use a dedicated folder per catalogue and extraction strategy.

Recommended:

```text
C:\Catalogue Results\
├── GEWISS-2025-26\
│   ├── Deterministic\
│   └── Difficult Pages 8B\
└── ROBUS-2026\
    ├── Deterministic\
    └── Difficult Pages 8B\
```

Do not mix two different catalogue PDFs in the same output folder.

The program creates missing output folders automatically.

---

## 40. Inspection points identified from the full catalogues

The pack includes:

```text
CATALOGUE_INSPECTION_REPORT.md
catalogue_inspection_points.csv
```

These are not the only possible failures. They are representative structural checks found by scanning both full catalogues.

High-priority examples include:

### GEWISS

```text
PDF 33   image-adjacent table and colour swatches
PDF 65   internal/right-hand SKU matrix and merged rows
PDF 503  many table blocks
PDF 708  colour/material SKU lists
PDF 1293 SKU index page
```

### ROBUS

```text
PDF 80   borderless dimensions
PDF 161  option-code table
PDF 165  SKUs as columns
PDF 448  many product blocks
PDF 507  dimensions and product matrices beside prose/icons
```

Use these pages for release testing, then still run the full catalogue.

---

# Part III — Troubleshooting

## Python is not recognised

Reinstall Python and select:

```text
Add python.exe to PATH
```

Then reopen PowerShell.

## PowerShell blocks the launcher

Run:

```powershell
powershell -ExecutionPolicy Bypass -File ".\run_extractor.ps1"
```

## The run appears stuck during layout

Check the latest log line.

A large page batch may take time. Reduce:

```text
--layout-batch-size 5
```

The existing batch checkpoints remain useful.

## Ollama times out

Use the 4B model and a longer timeout:

```text
--ollama-model qwen3-vl:4b-instruct
--ollama-timeout 1800
```

Use AI only for review-queue pages.

## Ollama context error

Use:

```text
--ollama-num-ctx 16384
```

## A product image is included as a column

Inspect `page_overlay.png`.

Use automatic edge trimming first. For a repeatable exception, add a manual override JSON.

## A ROBUS dimensions column is merged

Confirm that word-column repair is enabled. Do not use:

```text
--no-word-column-repair
```

Inspect `geometry.json` and `normalized_matrix.csv`.

## Option codes become unexpected SKUs

Check whether those values are in the canonical registry. Unknown short codes are review-only. Add a confirmed SKU to the registry only when it is a real product identifier.

## Many registry SKUs are unmatched

Check:

1. Correct catalogue and registry were paired.
2. `catalogue_page_mapping.json`.
3. Printed-page offset.
4. `sku_page_plan.csv`.
5. Whether the full catalogue or only registry-targeted pages were processed.
6. Whether some SKU information is non-tabular and therefore belongs in the future non-table extractor.

## Top-level CSV contains only exception pages

This can happen immediately after a review-queue pass. Run the complete command again with:

```text
--pages all --resume
```

to rebuild all catalogue-wide aggregates.

---

# Part IV — Boundaries of this pack

This program extracts tables and table-like matrices.

It intentionally does not yet consolidate:

- Features and benefits bullet lists
- Construction paragraphs
- Icon specification strips
- Product titles outside tables
- Warranty symbols
- QR-linked information
- Technical diagrams
- Narrative application notes

Those require a separate non-table product-page extractor and a different association model.

The table pack nevertheless preserves source-page and SKU occurrence information so its outputs can later be joined to that second process.
