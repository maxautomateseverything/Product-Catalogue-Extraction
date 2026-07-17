# Version 9 validation report

## Test source

`GEWISS(19-20).pdf`, two pages, using the uploaded GEWISS canonical SKU registry.

## Matrix recovery

The regression test deliberately supplied the malformed `lines_strict` matrix.
Version 9 compared it with local grid alternatives and selected `pymupdf_lines_1`
on both pages.

- Page 1 recovered 9 active leaf columns after removing one inactive grid column.
- Page 2 recovered 9 active leaf columns after removing one structural blank column.
- No SKU block was promoted into a header.
- Fast and screw wiring columns remained separate.

## Continuation

- Primary visual rows: 64, including alternative variant rows.
- Compressed specification row groups: 56.
- Secondary SKU rows: 56.
- Automatic continuation join: accepted.
- Join confidence: 1.00 in the standalone regression run.

## Reference assertions

The following attributes passed exact comparison for `GW 60 001 FH`,
`GW 60 001 H`, `GW 60 023 FH`, `GW 60 023 H`, and `GW 62 001 FH`:

- rated current
- rated voltage
- frequency
- poles
- reference h
- product type
- IP rating
- wiring type
- applications

`GW 60 005 WH` also passed:

- footnote marker
- footnote text
- phase inverter = Yes

No non-WH reference record received the phase-inverter attribute in the tested
output.

## Environment limitation

The dynamic relationship module was run against the real PDF using PyMuPDF.
The complete main PyMuPDF4LLM batching process was syntax-checked but could not
be executed here because `pymupdf4llm` is not installed in this environment.
The user's Windows environment already has that package from earlier runs.
