# Catalogue Contents Extractor — Full User Guide

## 1. What this pack does

This pack extracts structured rows from **catalogue contents pages**.

A catalogue contents page is usually near the front of a catalogue and tells the reader which page different product ranges or sections start on. It is different from a product-code index. A product-code index normally looks like:

```text
Product Code | Page
```

A contents page usually looks more like a hierarchy:

```text
Main category
  Sub category
    Product range or topic ........ page number
```

The output should be one row per **lowest-level contents item that points to a page**.

For example:

```text
Section 2 Cable Accessories
  Fixings & Fastenings
    Cable Ties ................................ 74
```

becomes:

| main_header | subheader_1 | subheader_2 | page_number |
|---|---|---|---|
| Section 2 Cable Accessories | Fixings & Fastenings | Cable Ties | 74 |

Header rows are not normally output as their own rows. They are used as context for the rows below or beside them.

---

## 2. Important assumptions

This process assumes:

1. The PDF has **selectable text**.
2. The contents pages are manually defined by the user.
3. Different groups of text can be identified by at least one of:
   - location on the page;
   - font name;
   - font size;
   - font height or width;
   - colour;
   - text pattern;
   - user-drawn regions.
4. The user reviews the output workbook and adjusts the config if needed.

This pack is not intended for scanned/image-only contents pages. If the page text cannot be selected in a PDF viewer, normal text extraction is unlikely to work.

---

## 3. Files in this pack

```text
catalogue_contents_extractor_pack/
  catalogue_contents_inspector.py
  catalogue_contents_extractor.py
  catalogue_contents_region_selector.py
  example_config_gewiss_contents.yaml
  example_config_ec_contents.yaml
  example_config_robus_contents.yaml
  CATALOGUE_CONTENTS_EXTRACTOR_GUIDE.md
  requirements.txt
```

| File | Purpose |
|---|---|
| `catalogue_contents_inspector.py` | Run first. It creates a workbook showing fonts, sizes, colours, line positions, spans, words, and suggested rules. |
| `catalogue_contents_extractor.py` | Main extractor. It uses a config file to create `contents.registry.csv` and review outputs. |
| `catalogue_contents_region_selector.py` | Optional visual selector. It lets the user draw regions on a PDF page. |
| `example_config_gewiss_contents.yaml` | Example config for a Gewiss-style hierarchical contents page. |
| `example_config_ec_contents.yaml` | Example config for an Electric Center-style dot-leader contents page. |
| `example_config_robus_contents.yaml` | Example config for a ROBUS-style visual card contents page. |
| `requirements.txt` | Python packages required by the pack. |

---

## 4. Installation

Open PowerShell in the folder containing the pack and run:

```powershell
py -m pip install -r requirements.txt
```

Required libraries:

```text
PyMuPDF
pdfplumber
pandas
openpyxl
PyYAML
Pillow
```

---

## 5. Standard user flow

For a new catalogue, follow this process.

```text
1. Identify the contents PDF pages.
2. Run the inspector on representative contents pages.
3. Open the inspection workbook.
4. Decide which extraction mode fits the catalogue.
5. Edit a config file.
6. If needed, use the region selector to draw regions.
7. Run the extractor.
8. Review contents.registry.csv, contents_rows.csv, and contents_review_workbook.xlsx.
9. Adjust config and rerun until validation examples pass.
```

---

## 6. Output files

The extractor writes these files to `output_folder`:

```text
contents.registry.csv
contents_rows.csv
contents_review_workbook.xlsx
contents_config_used.yaml
debug_images/
```

### 6.1 `contents.registry.csv`

This is the clean main output.

It contains only the requested output columns, for example:

```text
main_header, subheader_1, subheader_2, subheader_3, page_number
```

or:

```text
main_header, subheader_1, page_number
```

It does **not** include technical source/debug columns.

### 6.2 `contents_rows.csv`

This contains the extracted rows plus source columns.

Use it when checking how a row was created.

Important source columns:

| Column | Meaning |
|---|---|
| `source_pdf_page` | PDF page where the row was found. |
| `source_region` | Region/group that produced the row. |
| `source_line_ids` | Source line ids used for the row. |
| `raw_text` | Raw extracted text before cleaning/splitting. |
| `classification_rule` | Rule or mode that created the row. |
| `confidence_status` | `confirmed` or `needs_review`. |
| `review_reason` | Why review is needed. |

### 6.3 `contents_review_workbook.xlsx`

This is the main review file.

Sheets:

| Sheet | Purpose |
|---|---|
| `Run Summary` | Counts and high-level status. |
| `Page Diagnostics` | Page-by-page row counts and warnings. |
| `Extracted Contents Review` | All extracted rows with source details. |
| `Unresolved Rows` | Rows that need checking. |
| `Line Classification` | How lines were classified by rules. |
| `Rule Matches` | Lines that matched a non-ignore rule. |
| `Validation Examples` | Whether expected rows were found. |
| `Ignored Lines` | Lines ignored by ignore rules. |
| `Extractor Errors` | Errors encountered during extraction. |

### 6.4 `debug_images/`

Debug images show selected regions, card title/page regions, table-column regions, and issue pages.

---

## 7. Choosing the right extraction mode

The extractor supports three main modes.

| Mode | Use when | Typical catalogue |
|---|---|---|
| `style_rules` | Headings and item rows can be separated by font size, font, colour, or text pattern. | Simple contents pages. |
| `region_rules` | The page has multiple columns/areas, or non-contents areas must be ignored. | Electric Center / Gewiss-style pages. |
| `card_grid` | Contents are visual cards/tiles, with page numbers in boxes and titles beside them. | ROBUS-style contents pages. |

There is also an `area_table` approach inside `region_rules`. It is useful when a contents section is most accurately extracted by drawing separate column areas such as:

```text
subheader_2 column | subheader_3 column | page_number column
```

---

## 8. Catalogue contents types this pack can handle

### Type A — Headered hierarchical contents

Example structure:

```text
ENERGY
CIRCUIT BREAKERS FOR CIRCUIT PROTECTION
90 MCB Modular circuit breakers for circuit protection 436
```

Desired output:

| main_header | subheader_1 | subheader_2 | subheader_3 | page_number |
|---|---|---|---|---|
| ENERGY | CIRCUIT BREAKERS FOR CIRCUIT PROTECTION | 90 MCB | Modular circuit breakers for circuit protection | 436 - 460 |

Use:

```yaml
extraction:
  mode: region_rules
```

or use `area_table_groups` if the code/title/page number occupy predictable separate x regions.

### Type B — Dot-leader contents

Example:

```text
White Moulded Slim Line ................................12-15
Smart Accessories ........................................16
```

Desired output:

| main_header | subheader_1 | subheader_2 | page_number |
|---|---|---|---|
| Section 1 Wiring Accessories | Wiring Devices | White Moulded Slim Line | 12-15 |
| Section 1 Wiring Accessories | Wiring Devices | Smart Accessories | 16 |

Use:

```yaml
extraction:
  mode: region_rules
```

Key settings:

```yaml
page_range_derivation:
  enabled: false

multiline_items:
  enabled: true
  same_style_required: true
```

Do **not** derive page ranges here, because page ranges are explicitly printed in the contents page.

### Type C — Multi-line dot-leader contents

Example:

```text
Metal Driva Cavity
Fixings.............................96, 100-101
```

Desired output:

| main_header | subheader_1 | subheader_2 | page_number |
|---|---|---|---|
| Section 2 Cable Accessories | Fixings & Fastenings | Metal Driva Cavity Fixings | 96, 100-101 |

Use:

```yaml
multiline_items:
  enabled: true
  same_style_required: true
  max_vertical_gap: 8
```

The extractor will merge a line without a page number with the next matching-style line if the next line contains a page number.

### Type D — Visual card/grid contents

Example:

```text
171  DALLAS LED BACKLIT PANEL - IP65
198  ZIIGNA
212  ULTIMUM EXPRESS DUAL WATTAGE LED FIRE RATED
```

Desired output:

| main_header | subheader_1 | page_number |
|---|---|---|
| COMMERCIAL | DALLAS LED BACKLIT PANEL - IP65 | 171 |
| CUSTOMISED LINEAR | ZIIGNA | 198 - 211 |
| DOWNLIGHTS | ULTIMUM EXPRESS DUAL WATTAGE LED FIRE RATED | 212 - 213 |

Use:

```yaml
extraction:
  mode: card_grid
```

Draw or configure separate groups for each main header area.

---

## 9. Running the inspector

The inspector should be run before writing extraction rules.

### 9.1 Run from config

```powershell
py catalogue_contents_inspector.py --config example_config_gewiss_contents.yaml
```

### 9.2 Run directly

```powershell
py catalogue_contents_inspector.py --input "C:\Path\To\catalogue.pdf" --pages "13-14" --output "C:\Path\To\Inspection Output"
```

### 9.3 Inspector output

The inspector creates:

```text
contents_inspection_workbook.xlsx
inspection_debug_images/
```

Review these sheets:

| Sheet | What to look for |
|---|---|
| `Page Summary` | Confirms the page has selectable text. |
| `Line Records` | Every visual text line with coordinates and dominant style. |
| `Span Records` | Font, size, colour, flags, and bbox for text spans. |
| `Font Summary` | Which fonts/sizes/colours are common. |
| `Suggested Rules` | Possible header/item rules to copy into the config. |

---

## 10. Config file overview

A contents config usually contains:

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

### 10.1 Page range format

You can use:

```yaml
contents_pdf_pages: "13-14"
contents_pdf_pages: "4,6,8"
contents_pdf_pages: "4-6,9-10"
contents_pdf_pages: "all"
```

---

## 11. Output columns

Define the columns you want in `contents.registry.csv`:

```yaml
output_columns:
  - main_header
  - subheader_1
  - subheader_2
  - subheader_3
  - page_number
```

Supported common columns:

```text
main_header
subheader_1
subheader_2
subheader_3
page_number
subheader_2_page_number_combined
```

The extractor supports variable depth. ROBUS-style output can be:

```yaml
output_columns:
  - main_header
  - subheader_1
  - page_number
```

Electric Center-style output can include both combined and clean helper columns:

```yaml
output_columns:
  - main_header
  - subheader_1
  - subheader_2_page_number_combined
  - subheader_2
  - page_number

outputs:
  include_combined_item_page: true
```

---

## 12. Page-number settings

Use:

```yaml
page_number:
  regex: "(?i)(?:see\\s+page\\s+)?[A-Z]?\\d+(?:\\s*(?:,|/|;|-|–|—)\\s*[A-Z]?\\d+)*"
  position: "trailing"
```

Use "trailling" when page number is to the right and "leading " when to the left.

### 12.1 Supported page-number formats

The default regex supports:

```text
12
12-15
12, 19-20
13, 18, 22-23, 25, 27-31
A12
See page 12
```

### 12.2 Page-number position

| Position | Meaning | Example |
|---|---|---|
| `trailing` | Page number appears at the end of the item line. | `Cable Ties ........ 74` |
| `leading` | Page number appears before the item title. | `171 DALLAS PANEL` |
| `anywhere` | Extract a page number wherever it appears. | Use with care. |

---

## 13. Page-range derivation

Some catalogues print only the starting page for each product range.

Example:

```text
90 MCB Modular circuit breakers for circuit protection 436
RCD Modular circuit breakers for residual current protection 461
```

The first range should become:

```text
436 - 460
```

because the next range starts at `461`.

Enable this with:

```yaml
page_range_derivation:
  enabled: true
  scope_columns:
    - main_header
    - subheader_1
  same_rule_only: false
  last_item_strategy: "single_page"
```

Do **not** use this for catalogues that already print explicit ranges or lists, such as:

```text
12-15
12, 19-20
13, 18, 22-23, 25, 27-31
```

For those, use:

```yaml
page_range_derivation:
  enabled: false
```

---

## 14. Classification rules

Rules are applied in priority order. Lower priority numbers run first.

Example:

```yaml
classification_rules:
  - name: ignore_key_to_lamps
    priority: 1
    action: ignore
    text_regex: "^(Key to Lamps|Denotes)"

  - name: section_header
    priority: 10
    action: set_main_header
    text_regex: "^Section\\s+\\d+"

  - name: category_header
    priority: 20
    action: set_subheader_1
    font_size_min: 8
    color: "#F58220"

  - name: item_row
    priority: 100
    action: emit_row
    page_number_position: trailing
```

### 14.1 Rule filters

A rule can use these filters:

| Filter | Meaning |
|---|---|
| `text_regex` | Text must match regex. |
| `text_not_regex` | Text must not match regex. |
| `fontname_contains` | Font name must contain this text. |
| `font_size_min` | Minimum median font size. |
| `font_size_max` | Maximum median font size. |
| `font_height_min` | Minimum line height. |
| `font_height_max` | Maximum line height. |
| `font_width_min` | Minimum line width. |
| `font_width_max` | Maximum line width. |
| `color` | Dominant text colour as hex, such as `#F58220`. |
| `x0_min`, `x0_max` | Filter by left x coordinate. |
| `x1_min`, `x1_max` | Filter by right x coordinate. |
| `top_min`, `top_max` | Filter by top y coordinate. |
| `bottom_min`, `bottom_max` | Filter by bottom y coordinate. |
| `region_name` | Rule applies only inside one named content region. |
| `page_range` | Rule applies only to certain PDF pages. |

### 14.2 Rule actions

| Action | Meaning |
|---|---|
| `ignore` | Do not use this line. |
| `set_main_header` | Save this line as current `main_header`. |
| `set_subheader_1` | Save this line as current `subheader_1`. |
| `set_subheader_2` | Save this line as current `subheader_2`. |
| `set_subheader_3` | Save this line as current `subheader_3`. |
| `emit_row` | Create an output row. |

---

## 15. Multi-line item handling

Use this when item titles may wrap onto two lines.

```yaml
multiline_items:
  enabled: true
  same_style_required: true
  max_vertical_gap: 8
  mark_merged_rows_for_review: false
```

The extractor can merge:

```text
Metal Driva Cavity
Fixings.............................96, 100-101
```

into:

```text
Metal Driva Cavity Fixings.............................96, 100-101
```

The `same_style_required` option prevents unrelated headers from being merged with item rows.

---

## 16. Content regions and ignore regions

Use regions when the page has multiple columns, panels, or non-contents material.

```yaml
content_regions:
  - name: left_contents_column
    x0: 60
    y0: 100
    x1: 250
    y1: 700
    reading_order: 1

ignore_regions:
  - name: symbol_key
    x0: 500
    y0: 100
    x1: 700
    y1: 500
```

If `content_regions` are provided, the extractor ignores text outside them.

If `ignore_regions` are provided, the extractor ignores text inside them.

---

## 17. Area table groups

Use `area_table_groups` when the safest approach is to draw separate columns for the lowest-level rows.

This is useful for Gewiss-style rows where the code, title, and page number are visually separate:

```text
RK | Rigid protective conduit systems | 330
```

Config structure:

```yaml
area_table_groups:
  - name: installation_conduits
    page_range: "13"
    fixed_context:
      main_header: "INSTALLATION"
      subheader_1: "CONDUITS AND ACCESSORIES FOR INSTALLATION"
    row_tolerance: 3.0
    columns:
      - field: subheader_2
        x0: 100
        y0: 250
        x1: 140
        y1: 700
      - field: subheader_3
        x0: 145
        y0: 250
        x1: 450
        y1: 700
      - field: page_number
        x0: 460
        y0: 250
        x1: 520
        y1: 700
```

This produces rows like:

| main_header | subheader_1 | subheader_2 | subheader_3 | page_number |
|---|---|---|---|---|
| INSTALLATION | CONDUITS AND ACCESSORIES FOR INSTALLATION | RK | Rigid protective conduit systems | 330 - 343 |

when `page_range_derivation` is enabled.

---

## 18. Card grid mode

Use `card_grid` when the contents page is visual and made of cards or tiles.

Typical layout:

```text
[171]  DALLAS LED BACKLIT PANEL - IP65
[172]  SPACE LED UGR PANEL
```

Config:

```yaml
extraction:
  mode: "card_grid"

card_grid:
  row_tolerance: 12
  pairing_direction: "right"
  groups:
    - name: commercial_left_column
      page_range: "4"
      fixed_context:
        main_header: "COMMERCIAL"
      pairing_direction: "right"
      max_y_distance: 14
      page_number_regions:
        - x0: 20
          y0: 80
          x1: 55
          y1: 700
      title_regions:
        - x0: 60
          y0: 80
          x1: 180
          y1: 700
```

### 18.1 Pairing direction

| Direction | Meaning |
|---|---|
| `right` | Title is to the right of the page number. |
| `left` | Title is to the left of the page number. |
| `above` | Title is above the page number. |
| `below` | Title is below the page number. |

### 18.2 When main headers change on the same page

Create a separate card group for each main header area.

For example:

```yaml
card_grid:
  groups:
    - name: commercial_left_column
      fixed_context:
        main_header: "COMMERCIAL"
      ...

    - name: customised_linear_middle_column
      fixed_context:
        main_header: "CUSTOMISED LINEAR"
      ...

    - name: downlights_middle_column
      fixed_context:
        main_header: "DOWNLIGHTS"
      ...
```

This avoids guessing which card belongs to which header.

---

## 19. Visual region selector

The selector helps users create coordinates without typing x/y values manually.

### 19.1 Open selector

```powershell
py catalogue_contents_region_selector.py --pdf "C:\Path\To\catalogue.pdf" --config "contents_config.yaml" --page 4 --zoom 2.0 --output-template "contents_regions_page4.json" --output-config "contents_config_with_regions.yaml" --apply-pages "4"
```

### 19.2 What to draw

Depending on the catalogue type, draw different region types.

| Region type | Use for |
|---|---|
| `content_region` | Area where normal contents lines should be read. |
| `ignore_region` | Area to ignore, such as icon keys, footers, notes. |
| `table_column` | Column inside an `area_table_group`, such as `subheader_2`, `subheader_3`, `page_number`. |
| `card_page` | Page-number area in a card/grid layout. |
| `card_title` | Product-title area in a card/grid layout. |
| `card_region` | Whole card area for review/debug. |
| `header_region` | Header area for review or future enhancements. |

### 19.3 Fixed context

When drawing a region, the selector asks for fixed context JSON.

Example:

```json
{
  "main_header": "COMMERCIAL"
}
```

or:

```json
{
  "main_header": "INSTALLATION",
  "subheader_1": "CONDUITS AND ACCESSORIES FOR INSTALLATION"
}
```

This is important when a header visually applies to content beside it rather than directly below it.

---

## 20. Validation examples

Add known correct rows to the config.

```yaml
validation_examples:
  - main_header: "INSTALLATION"
    subheader_1: "CONDUITS AND ACCESSORIES FOR INSTALLATION"
    subheader_2: "RK"
    subheader_3: "Rigid protective conduit systems"
    page_number: "330 - 343"

  - main_header: "COMMERCIAL"
    subheader_1: "DALLAS LED BACKLIT PANEL - IP65"
    page_number: "171"
```

After extraction, open the `Validation Examples` sheet. Every row should have:

```text
status = found
```

Failed validation examples normally mean:

- a header was not assigned correctly;
- a region is wrong;
- page-range derivation is wrong;
- a row was not merged correctly;
- output text differs from expected text.

---

## 21. Worked example: Gewiss-style contents

### 21.1 Desired output

Example row:

| main_header | subheader_1 | subheader_2 | subheader_3 | page_number |
|---|---|---|---|---|
| INSTALLATION | CONDUITS AND ACCESSORIES FOR INSTALLATION | RK | Rigid protective conduit systems | 330 - 343 |

### 21.2 Recommended approach

Use one of these:

1. `region_rules` with font/style classification; or
2. `area_table_groups` with user-drawn columns.

For maximum accuracy, use `area_table_groups` where you draw:

```text
subheader_2/code column
subheader_3/title column
page_number column
```

Then set fixed context:

```yaml
fixed_context:
  main_header: "INSTALLATION"
  subheader_1: "CONDUITS AND ACCESSORIES FOR INSTALLATION"
```

Enable range derivation:

```yaml
page_range_derivation:
  enabled: true
  scope_columns:
    - main_header
    - subheader_1
```

---

## 22. Worked example: Electric Center-style contents

### 22.1 Desired output

| main_header | subheader_1 | subheader_2 | page_number |
|---|---|---|---|
| Section 2 Cable Accessories | Fixings & Fastenings | Metal Driva Cavity Fixings | 96, 100-101 |

### 22.2 Recommended approach

Use:

```yaml
extraction:
  mode: "region_rules"
```

Draw `content_region` areas around the contents columns and `ignore_region` areas around non-contents panels such as symbol keys.

Enable multiline merging:

```yaml
multiline_items:
  enabled: true
  same_style_required: true
  max_vertical_gap: 8
```

Do not derive page ranges:

```yaml
page_range_derivation:
  enabled: false
```

---

## 23. Worked example: ROBUS-style contents

### 23.1 Desired output

| main_header | subheader_1 | page_number |
|---|---|---|
| COMMERCIAL | DALLAS LED BACKLIT PANEL - IP65 | 171 |
| CUSTOMISED LINEAR | ZIIGNA | 198 - 211 |
| DOWNLIGHTS | ULTIMUM EXPRESS DUAL WATTAGE LED FIRE RATED | 212 - 213 |

### 23.2 Recommended approach

Use:

```yaml
extraction:
  mode: "card_grid"
```

Create one group for each category area:

```yaml
card_grid:
  groups:
    - name: commercial_left_column
      fixed_context:
        main_header: "COMMERCIAL"
      page_number_regions: [...]
      title_regions: [...]

    - name: customised_linear_middle_column
      fixed_context:
        main_header: "CUSTOMISED LINEAR"
      page_number_regions: [...]
      title_regions: [...]

    - name: downlights_middle_column
      fixed_context:
        main_header: "DOWNLIGHTS"
      page_number_regions: [...]
      title_regions: [...]
```

Enable page-range derivation if the catalogue prints only starting pages:

```yaml
page_range_derivation:
  enabled: true
  scope_columns:
    - main_header
```

---

## 24. Troubleshooting

### Problem: No rows extracted

Likely causes:

- wrong page range;
- page has no selectable text;
- content regions exclude all text;
- card/title/page regions are wrong;
- rules are too strict.

Actions:

1. Run the inspector.
2. Confirm `Page Summary.has_selectable_text = True`.
3. Check debug images.
4. Loosen font-size/colour filters.
5. Temporarily remove `content_regions` to test whole-page extraction.

### Problem: Headers assigned incorrectly

Likely causes:

- reading order is wrong;
- a header to the left applies to content on the right;
- a rule is matching the wrong lines.

Actions:

- Use separate content regions with `fixed_context`.
- Use `area_table_groups` for deterministic sections.
- Add validation examples.

### Problem: Wrapped item split into two rows

Likely causes:

- `multiline_items.enabled` is false;
- font/style differs between wrapped lines;
- vertical gap is larger than `max_vertical_gap`.

Actions:

```yaml
multiline_items:
  enabled: true
  same_style_required: true
  max_vertical_gap: 10
```

### Problem: Page ranges are wrong

Likely causes:

- `page_range_derivation` is enabled when page ranges are already printed;
- scope columns are too broad or too narrow;
- the rows are not in the correct reading order.

Actions:

- Disable range derivation for explicit range/list catalogues.
- Adjust `scope_columns`.
- Check `contents_rows.csv` extraction order.

### Problem: ROBUS titles pair with the wrong page number

Likely causes:

- page number and title regions are too large;
- `pairing_direction` is wrong;
- `max_y_distance` is too high.

Actions:

- Draw smaller regions.
- Set `pairing_direction: right` if title is to the right of page number.
- Lower `max_y_distance`.

---

## 25. What a good run looks like

A good run has:

```text
contents.registry.csv populated with expected rows
validation_examples all found
few or no unresolved rows
page diagnostics show row counts on all contents pages
no extractor errors
```

A run with some `needs_review` rows can still be useful. Review the workbook and decide whether the issue is acceptable or whether the config needs adjusting.

---

## 26. Recommended build process for a new catalogue

1. Copy the closest example config.
2. Edit `input_pdf`, `output_folder`, and `contents_pdf_pages`.
3. Run the inspector.
4. Review font, colour, and line records.
5. Choose mode:
   - `region_rules` for line-based contents.
   - `area_table_groups` for deterministic column areas.
   - `card_grid` for tile/card contents.
6. Use the selector if coordinates are needed.
7. Add 3 to 10 validation examples.
8. Run the extractor.
9. Review outputs.
10. Adjust and rerun.

---

## 27. Commands summary

### Install requirements

```powershell
py -m pip install -r requirements.txt
```

### Run inspector

```powershell
py catalogue_contents_inspector.py --config contents_config.yaml
```

### Open selector

```powershell
py catalogue_contents_region_selector.py --pdf "C:\Path\To\catalogue.pdf" --config "contents_config.yaml" --page 4 --zoom 2.0 --output-template "regions.json" --output-config "contents_config_with_regions.yaml" --apply-pages "4"
```

### Run extractor

```powershell
py catalogue_contents_extractor.py --config contents_config.yaml
```

---

## 28. Final note

The contents extractor is deliberately configurable. Contents pages vary more than index pages. The safest approach is:

```text
Inspect first.
Use styles and regions to make the structure explicit.
Validate known examples.
Review output before relying on it.
```
