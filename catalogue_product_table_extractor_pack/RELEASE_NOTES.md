# Version 9 release notes

- Replaces whole-table normalization with SKU-anchored relationship extraction.
- Scores competing layout, line and strict-line matrices by SKU preservation.
- Penalizes matrices that merge many SKU values into headers or single cells.
- Uses the canonical registry as authoritative SKU-anchor evidence.
- Searches all non-SKU attribute columns on both sides of every anchor.
- Crosses intervening SKU columns when collecting shared row attributes.
- Separates hierarchical column paths into product type, IP rating and wiring type.
- Emits candidate and accepted relationship CSVs with per-link confidence.
- Preserves unlabelled values as unresolved fields rather than discarding them.
- Retains true merged-cell inheritance and preserves genuine empty slots.
- Automatically applies high-confidence bidirectional continuation joins.
- Adds a numbered diagnostic artifact contract for proper evaluation.
