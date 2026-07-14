# Version 7 release notes

## Main changes

- Loads a canonical unique-SKU registry and an occurrence-level SKU index separately.
- Includes `needs_review` SKUs while preserving their review status.
- Automatically infers `PDF page = printed catalogue page + offset`.
- Builds a configurable nearby-page window around every indexed printed page.
- Treats index pointers as guidance rather than a restriction: exact SKU matches elsewhere are retained and flagged.
- Supports SKU codes in left, right, internal, multiple-row and column-header positions.
- Preserves each table's natural schema; a global AI schema is optional.
- Retains both original source headers and normalized attribute names.
- Produces unique product, occurrence and attribute datasets.
- Preserves all duplicate occurrences and reports conflicting attribute values.
- Imports pack/carton and pallet values from the SKU registry, retaining raw and parsed forms.
- Produces unmatched-registry, unexpected-PDF and fuzzy-candidate exception files.
- Adds editable review-queue input for exception-only reruns.
- Adds automatic page batching and per-table checkpointing for very large catalogues.
- Adds a guided PowerShell launcher and repeatable JSON configuration.
- Keeps the pack table-focused. Non-table product data is intentionally deferred to a separate extractor.
