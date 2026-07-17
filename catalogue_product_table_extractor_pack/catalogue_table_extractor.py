#!/usr/bin/env python3
"""Catalogue Table Extractor Version 9.

Extract product-catalogue tables from selected PDF pages.

Primary extractor
-----------------
PyMuPDF4LLM ``to_json()`` with layout analysis enabled. Table boxes are selected
from ``boxclass == \"table\"`` and their cell matrices are written to CSV/JSON.

Why the extra refinement step exists
------------------------------------
Catalogue pages often place a product photograph or caption immediately beside a
borderless table. A layout detector may therefore create a sparse, false edge
column covering that illustration. This script removes only sparse *edge*
columns, recalculates the crop from the retained cell geometry, and writes both
raw and refined diagnostics.

SKU-anchored relationship extraction
----------------------------------
The extractor does not require one global catalogue schema. Every exact registry
match becomes an SKU anchor. The relationship engine searches the anchor's
header path and all non-SKU attribute columns on the same visual row, crossing
other registry-backed SKU columns when required. It also compares line-grid
candidates and chooses the matrix that preserves SKU cells rather than merging
large code blocks into headers.

Optional routed local-LLM stage
-------------------------------
When ``--ollama-model`` is supplied, expensive vision calls are routed only to
suspicious boundaries by default. CSV structuring normally uses the compact
normalized text matrix, adding one table crop only for likely visual-only
columns such as colour swatches. ``--ai-review-policy always`` restores a full
boundary review for every table.

The available AI operations are:

Pass 1 -- optional review and refinement:
    The model sees the full page with boundary overlays, the current crop, and
    both raw and deterministic matrices. It evaluates whether edge columns are
    genuine table data, proposes corrected left/right column drops, estimates
    expected row/column counts, and reports extraction issues and confidence.
    A high-confidence proposal is applied to the original cell geometry and a
    new crop is rendered.

Pass 2 -- structuring:
    In ``--ai-mode structure``, the model sees the reviewed crop and matrix and
    converts them into table-specific columns and rows. A fixed schema is
    optional rather than required. The program writes
    the AI proposal, unresolved-cell list, confidence, quality report, and final
    CSV. Low-confidence AI output is gated by a configurable policy.

No PDF or image is sent to a cloud service by this script; the default Ollama
endpoint is localhost.

Install
-------
    python -m pip install pymupdf pymupdf4llm pillow

Example
-------
    python catalogue_table_extractor.py catalogue.pdf \
        --pages 30-40,65,80 \
        --output extracted_tables

With a local vision model:
    python catalogue_table_extractor.py catalogue.pdf \
        --pages 33 \
        --output extracted_tables \
        --ollama-model qwen3-vl:8b-instruct \
        --ai-mode structure

With a target schema:
    python catalogue_table_extractor.py catalogue.pdf \
        --pages 1-20 \
        --output extracted_tables \
        --ollama-model qwen3-vl:8b-instruct \
        --ai-mode structure \
        --schema product_schema.json

Schema file formats accepted:
    ["product_code", "description", "rated_current"]

or:
    {
      "columns": [
        {"name": "product_code", "description": "Exact catalogue code"},
        {"name": "rated_current", "description": "Current including unit"}
      ]
    }

The script always preserves raw extraction outputs so AI-produced data can be
audited against the source PDF.
"""

from __future__ import annotations

import argparse
import base64
import csv
import collections
import difflib
import datetime as _dt
import gc
import os
import subprocess
import shutil
import json
import logging
import re
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field

import dynamic_table_model as dtm
from pathlib import Path
from typing import Any, Iterable, Sequence

try:
    import pymupdf
    import pymupdf4llm
    from PIL import Image, ImageDraw
except ImportError as exc:  # pragma: no cover - friendly CLI failure
    raise SystemExit(
        "Missing dependency. Install with:\n"
        "  python -m pip install pymupdf pymupdf4llm pillow\n"
        f"Original error: {exc}"
    ) from exc

VERSION = "9.0"
LOGGER = logging.getLogger("catalogue-table-extractor")

LAYOUT_WORKER_CODE = r"""
import json
import os
import pathlib
import sys
import traceback
import pymupdf4llm

pdf_path = sys.argv[1]
json_path = pathlib.Path(sys.argv[2])
use_ocr = sys.argv[3] == "1"
error_path = pathlib.Path(sys.argv[4])
try:
    pymupdf4llm.use_layout(True)
    result = pymupdf4llm.to_json(pdf_path, use_ocr=use_ocr)
    json.loads(result)
    json_path.write_text(result, encoding="utf-8")
except Exception:
    error_path.write_text(traceback.format_exc(), encoding="utf-8")
    os._exit(2)
os._exit(0)
"""


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TargetColumn:
    name: str
    description: str = ""


@dataclass
class RefinedTable:
    raw_rows: list[list[str]]
    refined_rows: list[list[str]]
    raw_cells: list[list[list[float] | None]]
    refined_cells: list[list[list[float] | None]]
    raw_bbox: list[float]
    refined_bbox: list[float]
    dropped_left: int
    dropped_right: int
    raw_nulls: list[list[bool]] = field(default_factory=list)
    refined_nulls: list[list[bool]] = field(default_factory=list)
    source: str = "pymupdf4llm_layout"
    source_strategy: str = "layout"


@dataclass
class GridCandidate:
    rows: list[list[Any]]
    cells: list[list[list[float] | None]]
    bbox: list[float]
    strategy: str
    row_count: int
    col_count: int
    score: float = 0.0
    overlap: float = 0.0
    row_similarity: float = 0.0
    col_similarity: float = 0.0
    merged_placeholders: int = 0


# ---------------------------------------------------------------------------
# General helpers
# ---------------------------------------------------------------------------


def safe_slug(text: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "_", text).strip("._-")
    return slug or "catalogue"


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False), encoding="utf-8")


def write_csv(path: Path, rows: Sequence[Sequence[Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        for row in rows:
            writer.writerow(["" if value is None else value for value in row])


def clean_cell(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).replace("\u00ad", "").replace("\x00", "")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.split("\n")]
    return "\n".join(line for line in lines if line).strip()


def rectangularize(rows: Iterable[Iterable[Any]]) -> list[list[str]]:
    cleaned = [[clean_cell(cell) for cell in row] for row in rows]
    width = max((len(row) for row in cleaned), default=0)
    return [row + [""] * (width - len(row)) for row in cleaned]


def rectangularize_nulls(rows: Iterable[Iterable[Any]]) -> list[list[bool]]:
    materialized = [list(row) for row in rows]
    width = max((len(row) for row in materialized), default=0)
    return [
        [cell is None for cell in row] + [False] * (width - len(row))
        for row in materialized
    ]


def bbox_overlap_ratio(a: Sequence[float], b: Sequence[float]) -> float:
    """Intersection divided by the smaller rectangle area.

    This is intentionally more tolerant than IoU because a legacy grid finder
    may include an illustration column that the layout detector excludes.
    """
    ax0, ay0, ax1, ay1 = normalize_bbox(a)
    bx0, by0, bx1, by1 = normalize_bbox(b)
    ix0, iy0 = max(ax0, bx0), max(ay0, by0)
    ix1, iy1 = min(ax1, bx1), min(ay1, by1)
    intersection = max(0.0, ix1 - ix0) * max(0.0, iy1 - iy0)
    area_a = max(0.0, ax1 - ax0) * max(0.0, ay1 - ay0)
    area_b = max(0.0, bx1 - bx0) * max(0.0, by1 - by0)
    denominator = min(area_a, area_b)
    return intersection / denominator if denominator else 0.0


DEFAULT_PRODUCT_CODE_PATTERN = re.compile(
    r"(?i)^(?=[A-Z0-9 ._/-]{4,30}$)(?=.*[A-Z])(?=.*\d)"
    r"[A-Z]{1,8}[A-Z0-9]*(?:[ ._/-]?[A-Z0-9]{1,10}){1,5}$"
)


def looks_like_product_code(value: str, pattern: re.Pattern[str] | None = None) -> bool:
    text = clean_cell(value).replace("\n", " ").strip()
    if not text or len(text) > 40:
        return False
    lowered = text.casefold()
    if any(token in lowered for token in ("hz", "volt", "rated", "current", "warranty")):
        return False
    if any(symbol in text for symbol in ("÷", "+", "~", "%", ":")):
        return False
    return bool((pattern or DEFAULT_PRODUCT_CODE_PATTERN).fullmatch(text))



CODE_EXCLUSION_PATTERNS = (
    re.compile(r"(?i)^IP\d{2,3}$"),
    re.compile(r"(?i)^\d+(?:\.\d+)?(?:W|V|A|K|MM|CM|M|H|HZ|HRS|YRS|LMW)$"),
    re.compile(r"(?i)^\d+(?:/\d+)+$"),
    re.compile(r"(?i)^\d+\s*[x×]\s*\d+(?:A|W|V|MM|CM|M)?$"),
    re.compile(r"(?i)^\d+P(?:\+N)?\+E$"),
    re.compile(r"(?i)^(?:RGB|RGBW|CCT)\d*$"),
)


def normalize_code_key(value: str) -> str:
    """Normalise spacing and punctuation for product-code lookup."""
    return re.sub(r"[^A-Z0-9]", "", clean_cell(value).upper())


def plausible_registry_code(value: str) -> bool:
    """High-precision generic code test used only in labelled contexts."""
    text = re.sub(r"\s+", " ", clean_cell(value).replace("\n", " ")).strip()
    if not (4 <= len(text) <= 45):
        return False
    if not re.search(r"[A-Z]", text, re.IGNORECASE) or not re.search(r"\d", text):
        return False
    if any(pattern.fullmatch(text) for pattern in CODE_EXCLUSION_PATTERNS):
        return False
    if any(token in text.casefold() for token in (
        "rated", "voltage", "current", "length", "height", "width",
        "warranty", "product contents", "description",
    )):
        return False
    return bool(re.fullmatch(r"[A-Z0-9][A-Z0-9 ._/+()\-]{2,44}", text, re.IGNORECASE))


def _add_registry_entry(
    registry: dict[str, dict[str, Any]],
    code: str,
    *,
    confidence: float,
    source: str,
    page: int | None = None,
) -> None:
    display = re.sub(r"\s+", " ", clean_cell(code).replace("\n", " ")).strip()
    if not plausible_registry_code(display):
        return
    key = normalize_code_key(display)
    if not key:
        return
    entry = registry.setdefault(
        key,
        {
            "code": display,
            "normalized_code": key,
            "confidence": 0.0,
            "sources": set(),
            "pages": set(),
            "occurrences": 0,
        },
    )
    # Prefer the more informative representation, usually the one with spaces
    # or suffixes, while retaining a stable normalised key.
    if len(display) > len(str(entry["code"])):
        entry["code"] = display
    entry["confidence"] = max(float(entry["confidence"]), float(confidence))
    entry["sources"].add(source)
    if page is not None:
        entry["pages"].add(int(page))
    entry["occurrences"] += 1


def load_external_product_codes(
    path: Path | None,
    *,
    column_name: str | None = None,
) -> dict[str, dict[str, Any]]:
    """Load a previous code index/list from CSV, TSV, TXT or JSON.

    If no column is specified, every cell is inspected. This deliberately
    accepts sparse or differently named index exports.
    """
    registry: dict[str, dict[str, Any]] = {}
    if path is None:
        return registry
    if not path.exists():
        raise FileNotFoundError(path)

    suffix = path.suffix.casefold()
    values: list[str] = []
    if suffix == ".json":
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
        if isinstance(payload, dict):
            payload = payload.get("codes", payload.get("products", payload))
        if isinstance(payload, list):
            for item in payload:
                if isinstance(item, str):
                    values.append(item)
                elif isinstance(item, dict):
                    if column_name and column_name in item:
                        values.append(str(item[column_name]))
                    else:
                        values.extend(str(value) for value in item.values())
        elif isinstance(payload, dict):
            values.extend(str(value) for value in payload.values())
    elif suffix in {".txt", ".list"}:
        values.extend(path.read_text(encoding="utf-8-sig").splitlines())
    else:
        delimiter = "\t" if suffix == ".tsv" else ","
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle, delimiter=delimiter)
            if reader.fieldnames:
                if column_name and column_name not in reader.fieldnames:
                    raise ValueError(
                        f"Product-code column {column_name!r} was not found in "
                        f"{path.name}. Available columns: {reader.fieldnames}"
                    )
                for row in reader:
                    if column_name:
                        values.append(str(row.get(column_name, "")))
                    else:
                        values.extend(str(value or "") for value in row.values())
            else:
                handle.seek(0)
                for row in csv.reader(handle, delimiter=delimiter):
                    values.extend(row)

    for value in values:
        # A cell may contain several codes separated by newlines or semicolons.
        candidates = re.split(r"[\r\n;|]+", value)
        for candidate in candidates:
            _add_registry_entry(
                registry,
                candidate,
                confidence=1.0,
                source=f"external:{path.name}",
            )

    return registry


def _split_delimited_values(value: Any) -> list[str]:
    text = clean_cell(value).strip()
    if not text:
        return []
    return [item.strip() for item in re.split(r"[;\r\n|]+", text) if item.strip()]


def _split_int_values(value: Any) -> set[int]:
    values: set[int] = set()
    for item in _split_delimited_values(value):
        match = re.search(r"-?\d+", item)
        if match:
            try:
                values.add(int(match.group(0)))
            except ValueError:
                pass
    return values


def _ensure_registry_metadata(entry: dict[str, Any]) -> dict[str, Any]:
    entry.setdefault("confidence_statuses", set())
    entry.setdefault("catalogue_pages_original", set())
    entry.setdefault("catalogue_pages_normalized", set())
    entry.setdefault("source_pdf_pages", set())
    entry.setdefault("source_catalogue_pages", set())
    entry.setdefault("source_table_blocks", set())
    entry.setdefault("required_issues", set())
    entry.setdefault("optional_warnings", set())
    entry.setdefault("review_reasons", set())
    entry.setdefault("pack_carton_raw", set())
    entry.setdefault("pack_carton", set())
    entry.setdefault("pack_carton_statuses", set())
    entry.setdefault("pallet_raw", set())
    entry.setdefault("pallet", set())
    entry.setdefault("pallet_statuses", set())
    entry.setdefault("index_occurrences", [])
    entry.setdefault("canonical_registry", False)
    entry.setdefault("expected_pdf_pages", set())
    entry.setdefault("expected_pdf_page_core", set())
    return entry


def _update_registry_metadata_from_row(
    entry: dict[str, Any],
    row: dict[str, Any],
    *,
    occurrence_level: bool,
) -> None:
    _ensure_registry_metadata(entry)
    status = clean_cell(row.get("confidence_status", "")).strip() or "confirmed"
    entry["confidence_statuses"].add(status)
    entry["catalogue_pages_original"].update(
        _split_int_values(row.get("catalogue_page_original", row.get("catalogue_pages_original", "")))
    )
    entry["catalogue_pages_normalized"].update(
        _split_int_values(row.get("catalogue_page_normalized", row.get("catalogue_pages_normalized", "")))
    )
    entry["source_pdf_pages"].update(
        _split_int_values(row.get("source_pdf_page", row.get("source_pdf_pages", "")))
    )
    entry["source_catalogue_pages"].update(
        _split_int_values(row.get("source_catalogue_page", row.get("source_catalogue_pages", "")))
    )
    entry["source_table_blocks"].update(
        _split_int_values(row.get("source_table_block", row.get("source_table_blocks", "")))
    )
    for source_name, target_name in (
        ("required_issues", "required_issues"),
        ("optional_warnings", "optional_warnings"),
        ("review_reason", "review_reasons"),
        ("pack_carton_raw", "pack_carton_raw"),
        ("pack_carton", "pack_carton"),
        ("pack_carton_status", "pack_carton_statuses"),
        ("pack_carton_statuses", "pack_carton_statuses"),
        ("pallet_raw", "pallet_raw"),
        ("pallet", "pallet"),
        ("pallet_status", "pallet_statuses"),
        ("pallet_statuses", "pallet_statuses"),
    ):
        for value in _split_delimited_values(row.get(source_name, "")):
            entry[target_name].add(value)
    if occurrence_level:
        entry["index_occurrence_count"] = int(
            entry.get("index_occurrence_count", 0)
        ) + 1



def load_catalogue_sku_files(
    sku_registry_path: Path | None,
    index_rows_path: Path | None,
) -> dict[str, dict[str, Any]]:
    """Load the canonical unique-SKU registry and occurrence-level index rows.

    The unique registry is authoritative for SKU identity. Index rows add
    provenance, page pointers, pack/carton and pallet information. Rows marked
    ``needs_review`` remain included and are flagged in downstream outputs.
    """
    registry: dict[str, dict[str, Any]] = {}

    def read_csv_rows(path: Path) -> list[dict[str, Any]]:
        if not path.exists():
            raise FileNotFoundError(path)
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            return list(csv.DictReader(handle))

    if sku_registry_path:
        for row in read_csv_rows(sku_registry_path):
            code = clean_cell(row.get("sku", row.get("sku_raw", ""))).strip()
            if not code:
                continue
            status = clean_cell(row.get("confidence_status", "")).strip() or "confirmed"
            confidence = 0.80 if status == "needs_review" else 1.0
            _add_registry_entry(
                registry,
                code,
                confidence=confidence,
                source=f"sku_registry:{sku_registry_path.name}",
            )
            entry = registry[normalize_code_key(code)]
            entry["canonical_registry"] = True
            _update_registry_metadata_from_row(entry, row, occurrence_level=False)

    if index_rows_path:
        for row in read_csv_rows(index_rows_path):
            code = clean_cell(row.get("sku", row.get("sku_raw", ""))).strip()
            if not code:
                continue
            status = clean_cell(row.get("confidence_status", "")).strip() or "confirmed"
            confidence = 0.80 if status == "needs_review" else 1.0
            _add_registry_entry(
                registry,
                code,
                confidence=confidence,
                source=f"sku_index_rows:{index_rows_path.name}",
            )
            entry = registry[normalize_code_key(code)]
            entry["canonical_registry"] = True
            _update_registry_metadata_from_row(entry, row, occurrence_level=True)

    for entry in registry.values():
        _ensure_registry_metadata(entry)
        statuses = entry["confidence_statuses"]
        entry["registry_status"] = (
            "needs_review" if "needs_review" in statuses else "confirmed"
        )
    return registry


def infer_catalogue_page_offset(
    doc: pymupdf.Document,
    *,
    maximum_absolute_offset: int = 20,
) -> dict[str, Any]:
    """Infer ``PDF page = printed catalogue page + offset`` from page-edge text.

    The method scans standalone integers near the top or bottom edge of each
    page and selects the modal small offset. It is robust to occasional other
    numbers because genuine printed page numbers repeat across most pages.
    """
    counts: collections.Counter[int] = collections.Counter()
    samples: list[dict[str, int]] = []
    for page_index in range(doc.page_count):
        page = doc[page_index]
        height = float(page.rect.height)
        for word in page.get_text("words"):
            x0, y0, x1, y1, token = word[:5]
            value = str(token).strip()
            if not re.fullmatch(r"\d{1,4}", value):
                continue
            printed = int(value)
            if printed < 1 or printed > doc.page_count + maximum_absolute_offset:
                continue
            if not (float(y0) < height * 0.09 or float(y1) > height * 0.91):
                continue
            offset = (page_index + 1) - printed
            if abs(offset) > maximum_absolute_offset:
                continue
            counts[offset] += 1
            if len(samples) < 200:
                samples.append({
                    "pdf_page": page_index + 1,
                    "printed_page": printed,
                    "offset": offset,
                })
    if not counts:
        return {
            "offset": 0,
            "confidence": 0.0,
            "support": 0,
            "candidate_counts": {},
            "samples": [],
        }
    offset, support = counts.most_common(1)[0]
    total = sum(counts.values())
    return {
        "offset": int(offset),
        "confidence": round(support / max(1, total), 4),
        "support": int(support),
        "candidate_counts": {str(key): value for key, value in counts.most_common(10)},
        "samples": [sample for sample in samples if sample["offset"] == offset][:25],
    }


def apply_expected_page_windows(
    registry: dict[str, dict[str, Any]],
    *,
    offset: int,
    radius: int,
    page_count: int,
) -> None:
    for entry in registry.values():
        _ensure_registry_metadata(entry)
        core: set[int] = set()
        window: set[int] = set()
        for printed_page in entry.get("catalogue_pages_normalized", set()):
            pdf_page = int(printed_page) + int(offset)
            if 1 <= pdf_page <= page_count:
                core.add(pdf_page)
            for delta in range(-radius, radius + 1):
                candidate = pdf_page + delta
                if 1 <= candidate <= page_count:
                    window.add(candidate)
        entry["expected_pdf_page_core"] = core
        entry["expected_pdf_pages"] = window


def selected_pages_from_registry(
    registry: dict[str, dict[str, Any]],
    *,
    page_count: int,
) -> list[int]:
    pages = sorted({
        page
        for entry in registry.values()
        for page in entry.get("expected_pdf_pages", set())
        if 1 <= int(page) <= page_count
    })
    if not pages:
        raise ValueError(
            "The registry did not contain catalogue_page_normalized values that "
            "could be mapped to PDF pages."
        )
    return [page - 1 for page in pages]


def pages_from_review_queue(path: Path, *, page_count: int) -> list[int]:
    if not path.exists():
        raise FileNotFoundError(path)
    pages: set[int] = set()
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames or "page" not in reader.fieldnames:
            raise ValueError("Review queue must contain a 'page' column.")
        for row in reader:
            status = clean_cell(row.get("review_status", "")).strip().casefold()
            if status in {"resolved", "approved", "ignore", "closed"}:
                continue
            try:
                page = int(float(clean_cell(row.get("page", ""))))
            except ValueError:
                continue
            if 1 <= page <= page_count:
                pages.add(page)
    if not pages:
        raise ValueError("The review queue contained no open, valid PDF pages.")
    return [page - 1 for page in sorted(pages)]


def discover_pdf_product_codes(
    doc: pymupdf.Document,
    *,
    page_indexes: Sequence[int] | None = None,
) -> dict[str, dict[str, Any]]:
    """Build a high-confidence code registry from selectable PDF text.

    The scanner uses:
    - GEWISS-style ``GW 12 345`` codes anywhere in native text;
    - values immediately following a standalone ``PRODUCT`` label;
    - first-column candidates in ``DIMENSIONS`` blocks.

    It does not use OCR and is much faster than running a vision model.
    """
    registry: dict[str, dict[str, Any]] = {}
    indexes = list(page_indexes) if page_indexes is not None else list(range(doc.page_count))
    gewiss_pattern = re.compile(
        r"\bGW\s*\d{2}\s*\d{3}(?:\s+(?!GW\b)[A-Z]{1,3}(?:\d{1,3})?)?\b",
        re.IGNORECASE,
    )

    for offset, page_index in enumerate(indexes, start=1):
        if offset == 1 or offset % 100 == 0:
            LOGGER.info(
                "Product-code registry scan: page %s of %s.",
                offset,
                len(indexes),
            )
        page_number = page_index + 1
        text = doc[page_index].get_text("text")
        lines = [re.sub(r"\s+", " ", line.strip()) for line in text.splitlines()]
        nonempty = [line for line in lines if line]

        for match in gewiss_pattern.finditer(text):
            _add_registry_entry(
                registry,
                match.group(0),
                confidence=0.99,
                source="gewiss_pattern",
                page=page_number,
            )

        for index, line in enumerate(nonempty):
            upper = line.upper()
            if upper == "PRODUCT" and index + 1 < len(nonempty):
                _add_registry_entry(
                    registry,
                    nonempty[index + 1],
                    confidence=0.98,
                    source="product_label",
                    page=page_number,
                )
            elif upper.startswith("PRODUCT "):
                _add_registry_entry(
                    registry,
                    line[8:].strip(),
                    confidence=0.98,
                    source="product_label",
                    page=page_number,
                )

        # ROBUS and many lighting catalogues put a compact borderless table
        # after a DIMENSIONS heading. The first item of each repeated numeric
        # row is the product code.
        dimension_indexes = [
            index for index, line in enumerate(nonempty)
            if line.upper().startswith("DIMENSIONS")
        ]
        for dimension_index in dimension_indexes:
            block = nonempty[dimension_index + 1 : dimension_index + 80]
            numeric_run = 0
            for index, value in enumerate(block):
                upper = value.upper()
                if upper in {
                    "PRODUCT", "FEATURES & BENEFITS:", "CONSTRUCTION:",
                    "ACCESSORIES:", "TECHNICAL INFORMATION:",
                }:
                    break
                if plausible_registry_code(value):
                    following = block[index + 1 : index + 5]
                    numeric_followers = sum(
                        bool(re.fullmatch(r"[-+]?\d+(?:[.,]\d+)?", item))
                        for item in following
                    )
                    if numeric_followers >= 2:
                        _add_registry_entry(
                            registry,
                            value,
                            confidence=0.96,
                            source="dimensions_first_column",
                            page=page_number,
                        )
                        numeric_run += 1
                elif numeric_run and re.search(r"[A-Za-z]", value) and ":" in value:
                    break

    return registry


def merge_code_registries(
    *registries: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    metadata_set_fields = (
        "confidence_statuses",
        "catalogue_pages_original",
        "catalogue_pages_normalized",
        "source_pdf_pages",
        "source_catalogue_pages",
        "source_table_blocks",
        "required_issues",
        "optional_warnings",
        "review_reasons",
        "pack_carton_raw",
        "pack_carton",
        "pack_carton_statuses",
        "pallet_raw",
        "pallet",
        "pallet_statuses",
        "expected_pdf_pages",
        "expected_pdf_page_core",
    )
    for registry in registries:
        for entry in registry.values():
            key = str(entry["normalized_code"])
            current = merged.setdefault(
                key,
                {
                    "code": entry["code"],
                    "normalized_code": key,
                    "confidence": 0.0,
                    "sources": set(),
                    "pages": set(),
                    "occurrences": 0,
                    "canonical_registry": False,
                    "index_occurrences": [],
                },
            )
            _ensure_registry_metadata(current)
            if len(str(entry["code"])) > len(str(current["code"])):
                current["code"] = entry["code"]
            current["confidence"] = max(
                float(current["confidence"]),
                float(entry.get("confidence", 0.0)),
            )
            current["sources"].update(entry.get("sources", set()))
            current["pages"].update(entry.get("pages", set()))
            current["occurrences"] += int(entry.get("occurrences", 0))
            current["canonical_registry"] = bool(
                current.get("canonical_registry") or entry.get("canonical_registry")
            )
            for field_name in metadata_set_fields:
                current[field_name].update(entry.get(field_name, set()))
            current["index_occurrences"].extend(entry.get("index_occurrences", []))
            statuses = current.get("confidence_statuses", set())
            current["registry_status"] = (
                "needs_review" if "needs_review" in statuses else "confirmed"
            )
    return merged


def serialise_code_registry(
    registry: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for entry in registry.values():
        _ensure_registry_metadata(entry)
        rows.append(
            {
                "code": entry["code"],
                "normalized_code": entry["normalized_code"],
                "confidence": round(float(entry["confidence"]), 3),
                "registry_status": entry.get("registry_status", "discovered"),
                "canonical_registry": bool(entry.get("canonical_registry")),
                "sources": sorted(entry["sources"]),
                "pages": sorted(entry["pages"]),
                "catalogue_pages_original": sorted(entry["catalogue_pages_original"]),
                "catalogue_pages_normalized": sorted(entry["catalogue_pages_normalized"]),
                "source_pdf_pages": sorted(entry["source_pdf_pages"]),
                "expected_pdf_page_core": sorted(entry["expected_pdf_page_core"]),
                "expected_pdf_pages": sorted(entry["expected_pdf_pages"]),
                "occurrences": int(entry["occurrences"]),
                "pack_carton_raw": sorted(entry["pack_carton_raw"]),
                "pack_carton": sorted(entry["pack_carton"]),
                "pallet_raw": sorted(entry["pallet_raw"]),
                "pallet": sorted(entry["pallet"]),
                "required_issues": sorted(entry["required_issues"]),
                "optional_warnings": sorted(entry["optional_warnings"]),
                "review_reasons": sorted(entry["review_reasons"]),
            }
        )
    return sorted(rows, key=lambda item: item["normalized_code"])


def load_serialised_code_registry(path: Path) -> dict[str, dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError(f"Serialized registry {path} must contain a JSON list.")
    registry: dict[str, dict[str, Any]] = {}
    set_fields = (
        "sources",
        "pages",
        "catalogue_pages_original",
        "catalogue_pages_normalized",
        "source_pdf_pages",
        "expected_pdf_page_core",
        "expected_pdf_pages",
        "pack_carton_raw",
        "pack_carton",
        "pallet_raw",
        "pallet",
        "required_issues",
        "optional_warnings",
        "review_reasons",
    )
    for item in payload:
        key = str(item.get("normalized_code", ""))
        if not key:
            continue
        entry: dict[str, Any] = {
            "code": item.get("code", key),
            "normalized_code": key,
            "confidence": float(item.get("confidence", 0.0)),
            "registry_status": item.get("registry_status", "discovered"),
            "canonical_registry": bool(item.get("canonical_registry")),
            "occurrences": int(item.get("occurrences", 0)),
            "confidence_statuses": {item.get("registry_status", "discovered")},
            "source_catalogue_pages": set(),
            "source_table_blocks": set(),
            "pack_carton_statuses": set(),
            "pallet_statuses": set(),
            "index_occurrences": [],
        }
        for field_name in set_fields:
            entry[field_name] = set(item.get(field_name, []) or [])
        _ensure_registry_metadata(entry)
        registry[key] = entry
    return registry


def codes_in_text(
    value: str,
    registry: dict[str, dict[str, Any]],
    *,
    fallback_pattern: re.Pattern[str] | None = None,
) -> list[str]:
    """Return known product codes present in a cell.

    Contiguous token n-grams allow matching spaced codes such as ``GW 60 082``
    without constructing a huge regular-expression alternation.
    """
    text = re.sub(r"\s+", " ", clean_cell(value).replace("\n", " ")).strip()
    if not text:
        return []
    tokens = re.findall(r"[A-Za-z0-9][A-Za-z0-9._/+()\-]*", text)
    found: list[str] = []
    seen: set[str] = set()
    max_ngram = min(6, len(tokens))
    for size in range(max_ngram, 0, -1):
        for start in range(0, len(tokens) - size + 1):
            candidate = " ".join(tokens[start : start + size])
            key = normalize_code_key(candidate)
            if key in registry and key not in seen:
                found.append(str(registry[key]["code"]))
                seen.add(key)

    if not found and looks_like_product_code(text, fallback_pattern):
        key = normalize_code_key(text)
        if key in registry:
            found.append(str(registry[key]["code"]))
        elif not registry:
            found.append(text)
    return found


def section_context_value(
    row: Sequence[str],
    pattern: re.Pattern[str] | None = None,
) -> str | None:
    values = [re.sub(r"\s+", " ", clean_cell(value).replace("\n", " ")).strip() for value in row]
    values = [value for value in values if value]
    if not values or any(looks_like_product_code(value, pattern) for value in values):
        return None
    joined = " ".join(values)
    sparse_limit = max(2, max(1, len(row) // 3))
    context_terms = re.search(
        r"(?i)\b(rated|current|range|series|version|variant|size|class|category|group)\b",
        joined,
    )
    if len(values) <= sparse_limit and (":" in joined or context_terms):
        joined = re.sub(r"\(\s+", "(", joined)
        joined = re.sub(r"\s+\)", ")", joined)
        joined = re.sub(r"\s*:\s*", ": ", joined)
        return joined.strip()
    return None


def make_unique_headers(headers: Sequence[str]) -> list[str]:
    counts: dict[str, int] = {}
    output: list[str] = []
    for index, value in enumerate(headers, start=1):
        base = re.sub(r"\s+", " ", clean_cell(value).replace("\n", " ")).strip()
        if not base:
            base = f"column_{index}"
        key = base.casefold()
        counts[key] = counts.get(key, 0) + 1
        output.append(base if counts[key] == 1 else f"{base}_{counts[key]}")
    return output


def expand_merged_values(table: RefinedTable) -> tuple[list[list[str]], list[dict[str, Any]]]:
    """Expand true PDF merged-cell placeholders horizontally and vertically.

    PyMuPDF's grid table extractor represents cells covered by a merged cell as
    ``None``. An actual empty cell is normally ``""``. Keeping that distinction
    lets us duplicate values such as "Without cable", "16 A", and a voltage
    range only into rows to which the visual cell genuinely applies.
    """
    rows = [list(row) for row in table.refined_rows]
    nulls = table.refined_nulls or [[False] * len(row) for row in rows]
    events: list[dict[str, Any]] = []
    if not rows:
        return rows, events

    # Horizontal colspan expansion, mainly for multi-level header groups.
    for row_index, row in enumerate(rows):
        last_value = ""
        last_origin = -1
        for column_index, value in enumerate(row):
            if value:
                last_value = value
                last_origin = column_index
            elif column_index < len(nulls[row_index]) and nulls[row_index][column_index]:
                if last_value:
                    row[column_index] = last_value
                    events.append(
                        {
                            "direction": "horizontal",
                            "row": row_index + 1,
                            "column": column_index + 1,
                            "origin_column": last_origin + 1,
                            "value": last_value,
                        }
                    )
            else:
                last_value = ""
                last_origin = -1

    # Vertical rowspan expansion. Real blank cells interrupt the propagation.
    width = max((len(row) for row in rows), default=0)
    for column_index in range(width):
        last_value = ""
        last_origin = -1
        for row_index, row in enumerate(rows):
            value = row[column_index] if column_index < len(row) else ""
            is_null = (
                row_index < len(nulls)
                and column_index < len(nulls[row_index])
                and nulls[row_index][column_index]
            )
            if value:
                last_value = value
                last_origin = row_index
            elif is_null and last_value:
                row[column_index] = last_value
                events.append(
                    {
                        "direction": "vertical",
                        "row": row_index + 1,
                        "column": column_index + 1,
                        "origin_row": last_origin + 1,
                        "value": last_value,
                    }
                )
            else:
                last_value = ""
                last_origin = -1

    return rows, events


def deterministic_records(
    rows: list[list[str]],
    original_rows: list[list[str]],
    *,
    product_code_pattern: re.Pattern[str] | None = None,
) -> tuple[list[str], list[list[str]], dict[str, Any]]:
    """Convert a normalized visual matrix into a conservative rectangular CSV.

    The routine does not attempt manufacturer-specific semantic mapping. It
    flattens initial multi-row headers, removes single-cell section label rows,
    and carries those labels into a ``section_context`` column. Product rows are
    identified using a deliberately conservative catalogue-code heuristic.
    """
    if not rows:
        return [], [], {"header_rows": 0, "section_rows": 0, "data_rows": 0}

    pattern = product_code_pattern or DEFAULT_PRODUCT_CODE_PATTERN
    first_data = None
    for index, row in enumerate(rows):
        if any(looks_like_product_code(value, pattern) for value in row):
            first_data = index
            break
    if first_data is None:
        first_data = 1 if len(rows) > 1 else 0

    # A single-cell row immediately before the first product is a section label,
    # not a column header (for example "Rated current (A): 16").
    header_count = first_data
    for index in range(first_data):
        if section_context_value(original_rows[index], pattern) is not None:
            header_count = index
            break

    header_rows = rows[:header_count]
    width = max((len(row) for row in rows), default=0)
    header_values: list[str] = []
    for column_index in range(width):
        pieces: list[str] = []
        for row in header_rows:
            value = row[column_index] if column_index < len(row) else ""
            value = re.sub(r"\s+", " ", value.replace("\n", " ")).strip()
            if value and value.casefold() not in {item.casefold() for item in pieces}:
                pieces.append(value)
        header_values.append(" | ".join(pieces))
    columns = make_unique_headers(header_values)

    output_rows: list[list[str]] = []
    current_context = ""
    section_rows = 0
    for row_index in range(header_count, len(rows)):
        row = list(rows[row_index]) + [""] * (width - len(rows[row_index]))
        original = (
            list(original_rows[row_index]) + [""] * (width - len(original_rows[row_index]))
        )
        has_code = any(looks_like_product_code(value, pattern) for value in row)
        section_value = section_context_value(original, pattern)
        if section_value is not None and not has_code:
            current_context = section_value
            section_rows += 1
            continue
        if not any(value for value in row):
            continue
        record_row = [
            re.sub(r"\s+", " ", value.replace("\n", " ")).strip()
            for value in row
        ]
        output_rows.append(record_row + ([current_context] if current_context else [""]))

    if any(row and row[-1] for row in output_rows):
        columns.append("section_context")
    else:
        output_rows = [row[:width] for row in output_rows]

    return columns, output_rows, {
        "header_rows": header_count,
        "section_rows": section_rows,
        "data_rows": len(output_rows),
    }



GENERIC_CODE_HEADERS = {
    "code", "product code", "product", "sku", "catalogue no", "catalogue number",
    "reference", "item", "item code", "part number", "model",
    "dimensions (mm)", "dimensions",
}


def _normalise_attribute_name(value: str, fallback: str) -> str:
    name = re.sub(r"\s+", " ", clean_cell(value).replace("\n", " ")).strip()
    return name or fallback


def _merge_attribute(attributes: dict[str, str], name: str, value: str) -> None:
    name = _normalise_attribute_name(name, "attribute")
    value = re.sub(r"\s+", " ", clean_cell(value).replace("\n", " ")).strip()
    if not value:
        return
    if name not in attributes:
        attributes[name] = value
        return
    existing = [item.strip() for item in attributes[name].split(" | ")]
    if value not in existing:
        attributes[name] = attributes[name] + " | " + value


ATTRIBUTE_ALIASES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)^rated\s*current(?:\s*\([^)]*\))?$"), "rated_current"),
    (re.compile(r"(?i)^rated\s*voltage(?:\s*\([^)]*\))?$"), "rated_voltage"),
    (re.compile(r"(?i)^(?:no\.\s*of\s*)?poles?$"), "poles"),
    (re.compile(r"(?i)^reference\s*h$"), "reference_h"),
    (re.compile(r"(?i)^frequency$"), "frequency"),
    (re.compile(r"(?i)^colou?r$"), "colour"),
    (re.compile(r"(?i)^pack(?:\s*/?\s*carton)?$"), "pack_carton"),
    (re.compile(r"(?i)^carton\s*qty$"), "carton_quantity"),
    (re.compile(r"(?i)^length(?:\s*\(mm\))?$"), "length"),
    (re.compile(r"(?i)^width(?:\s*\(mm\))?$"), "width"),
    (re.compile(r"(?i)^height(?:\s*/\s*depth)?(?:\s*\(mm\))?$"), "height_depth"),
    (re.compile(r"(?i)^diameter(?:\s*\(mm\))?$"), "diameter"),
    (re.compile(r"(?i)^description$"), "description"),
    (re.compile(r"(?i)^type$"), "product_type"),
    (re.compile(r"(?i)^section_context$"), "section_context"),
    (re.compile(r"(?i)^code_column$"), "output_configuration"),
)


def normalize_attribute_name(source_header: str) -> str:
    source = _normalise_attribute_name(source_header, "attribute")
    for pattern, replacement in ATTRIBUTE_ALIASES:
        if pattern.fullmatch(source):
            return replacement
    normalized = re.sub(r"[^a-z0-9]+", "_", source.casefold()).strip("_")
    return normalized or "attribute"


def _record_attribute(
    record: dict[str, Any],
    *,
    source_header: str,
    value: str,
    source_row: int | None,
    source_column: int | None,
    inherited: bool = False,
) -> None:
    clean_value = re.sub(r"\s+", " ", clean_cell(value).replace("\n", " ")).strip()
    if not clean_value:
        return
    source_name = _normalise_attribute_name(source_header, "attribute")
    normalized_name = normalize_attribute_name(source_name)
    _merge_attribute(record["attributes"], source_name, clean_value)
    observations = record.setdefault("attribute_observations", [])
    candidate = {
        "source_attribute": source_name,
        "normalized_attribute": normalized_name,
        "value": clean_value,
        "source_row": source_row,
        "source_column": source_column,
        "inherited": bool(inherited),
    }
    if candidate not in observations:
        observations.append(candidate)


def _unknown_code_candidate(value: str, header: str, column_index: int, width: int) -> str | None:
    text = re.sub(r"\s+", " ", clean_cell(value).replace("\n", " ")).strip()
    if not text or not plausible_registry_code(text):
        return None
    header_key = header.casefold()
    header_is_code = (
        header_key in GENERIC_CODE_HEADERS
        or re.search(r"(?i)\b(code|sku|product|model|part number|catalogue)\b", header)
        is not None
    )
    letter_count = len(re.findall(r"[A-Za-z]", text))
    digit_count = len(re.findall(r"\d", text))
    strong_code_shape = letter_count >= 2 and digit_count >= 3
    if header_is_code or strong_code_shape:
        return text
    return None


def dynamic_product_records(
    columns: Sequence[str],
    rows: Sequence[Sequence[str]],
    *,
    registry: dict[str, dict[str, Any]],
    product_code_pattern: re.Pattern[str] | None,
    page_number: int,
    table_number: int,
    method: str,
) -> list[dict[str, Any]]:
    """Create schema-flexible product records from any code orientation.

    Supported orientations:
    - product code in the first, last, or an internal data column;
    - several product codes in one visual row;
    - product codes used as column headers, with attributes down the rows.

    Exact and normalized registry matches are accepted automatically. A
    code-like value absent from the registry is retained only in a clearly
    code-labelled or edge-index position and is marked ``unexpected_pdf_sku``.
    Fuzzy matching is performed later and never silently confirms identity.
    """
    headers = [
        _normalise_attribute_name(value, f"column_{index}")
        for index, value in enumerate(columns, start=1)
    ]
    width = len(headers)
    materialized = [
        list(row) + [""] * max(0, width - len(row))
        for row in rows
    ]

    def cell_codes(value: str, column_index: int, header: str) -> list[tuple[str, bool]]:
        known = codes_in_text(value, registry, fallback_pattern=product_code_pattern)
        if known:
            return [(code, False) for code in known]
        unknown = _unknown_code_candidate(value, header, column_index, width)
        if unknown:
            return [(unknown, True)]
        return []

    code_headers: dict[int, list[tuple[str, bool]]] = {}
    for index, header in enumerate(headers):
        known = codes_in_text(header, registry, fallback_pattern=product_code_pattern)
        if known:
            code_headers[index] = [(code, False) for code in known]
        elif plausible_registry_code(header):
            code_headers[index] = [(header, normalize_code_key(header) not in registry)]

    records_by_key: dict[tuple[str, str, int | None, int | None], dict[str, Any]] = {}

    def ensure_record(
        code: str,
        role: str,
        source_row: int | None,
        source_column: int | None,
        *,
        unexpected: bool,
    ) -> dict[str, Any]:
        key = (normalize_code_key(code), role, source_row, source_column)
        record = records_by_key.get(key)
        if record is None:
            normalized = normalize_code_key(code)
            registry_entry = registry.get(normalized, {})
            record = {
                "product_code": str(registry_entry.get("code", code)),
                "product_code_raw": code,
                "product_code_normalized": normalized,
                "code_position": role,
                "attributes": {},
                "attribute_observations": [],
                "source_page": page_number,
                "source_table": table_number,
                "source_rows": [],
                "source_columns": [],
                "method": method,
                "registry_match": "unexpected_pdf_sku" if unexpected else "exact_normalized",
                "registry_status": registry_entry.get("registry_status", "unregistered"),
                "registry_canonical": bool(registry_entry.get("canonical_registry")),
            }
            records_by_key[key] = record
        if source_row is not None and source_row not in record["source_rows"]:
            record["source_rows"].append(source_row)
        if source_column is not None and source_column not in record["source_columns"]:
            record["source_columns"].append(source_column)
        return record

    # Row-oriented products.
    for row_index, row in enumerate(materialized, start=1):
        occurrences: list[tuple[int, str, bool]] = []
        for column_index, value in enumerate(row):
            for code, unexpected in cell_codes(value, column_index, headers[column_index]):
                occurrences.append((column_index, code, unexpected))
        if not occurrences:
            continue

        for code_column, code, unexpected in occurrences:
            if code_column == 0:
                role = "left_row_index"
            elif code_column == width - 1:
                role = "right_row_index"
            else:
                role = "internal_matrix_cell"
            if len(occurrences) > 1:
                role = "multiple_codes_in_row"
            record = ensure_record(
                code,
                role,
                row_index,
                code_column + 1,
                unexpected=unexpected,
            )

            code_header = headers[code_column]
            if code_header.casefold() not in GENERIC_CODE_HEADERS and not codes_in_text(
                code_header, registry, fallback_pattern=product_code_pattern
            ):
                _record_attribute(
                    record,
                    source_header="code_column",
                    value=code_header,
                    source_row=row_index,
                    source_column=code_column + 1,
                )

            for column_index, value in enumerate(row):
                if column_index == code_column or not value:
                    continue
                if cell_codes(value, column_index, headers[column_index]):
                    continue
                _record_attribute(
                    record,
                    source_header=headers[column_index],
                    value=value,
                    source_row=row_index,
                    source_column=column_index + 1,
                )

    # Column-oriented products.
    for code_column, codes in code_headers.items():
        for code, unexpected in codes:
            record = ensure_record(
                code,
                "column_header_index",
                None,
                code_column + 1,
                unexpected=unexpected,
            )
            for row_index, row in enumerate(materialized, start=1):
                if code_column >= len(row):
                    continue
                value = row[code_column]
                if not value or cell_codes(value, code_column, headers[code_column]):
                    continue

                row_label = ""
                label_column: int | None = None
                for candidate_index in range(0, code_column):
                    candidate = row[candidate_index]
                    if candidate and not cell_codes(
                        candidate, candidate_index, headers[candidate_index]
                    ):
                        row_label = candidate
                        label_column = candidate_index + 1
                        break
                if not row_label:
                    for candidate_index, candidate in enumerate(row):
                        if candidate_index == code_column:
                            continue
                        if candidate and not cell_codes(
                            candidate, candidate_index, headers[candidate_index]
                        ):
                            row_label = candidate
                            label_column = candidate_index + 1
                            break
                attribute_name = _normalise_attribute_name(
                    row_label,
                    f"row_{row_index}",
                )
                _record_attribute(
                    record,
                    source_header=attribute_name,
                    value=value,
                    source_row=row_index,
                    source_column=code_column + 1,
                )
                if label_column and label_column not in record["source_columns"]:
                    record["source_columns"].append(label_column)

    return sorted(
        records_by_key.values(),
        key=lambda item: (
            normalize_code_key(str(item["product_code"])),
            str(item["code_position"]),
            item["source_rows"][0] if item["source_rows"] else -1,
            item["source_columns"][0] if item["source_columns"] else -1,
        ),
    )


def product_records_wide(
    records: Sequence[dict[str, Any]],
) -> tuple[list[str], list[list[str]]]:
    attribute_names = sorted(
        {
            name
            for record in records
            for name in record.get("attributes", {}).keys()
        },
        key=str.casefold,
    )
    columns = [
        "product_code",
        "code_position",
        "source_page",
        "source_table",
        "source_rows",
        "source_columns",
        *attribute_names,
    ]
    rows: list[list[str]] = []
    for record in records:
        attributes = record.get("attributes", {})
        rows.append(
            [
                str(record.get("product_code", "")),
                str(record.get("code_position", "")),
                str(record.get("source_page", "")),
                str(record.get("source_table", "")),
                ";".join(str(value) for value in record.get("source_rows", [])),
                ";".join(str(value) for value in record.get("source_columns", [])),
                *[str(attributes.get(name, "")) for name in attribute_names],
            ]
        )
    return columns, rows


def product_records_long_rows(
    pdf_name: str,
    records: Sequence[dict[str, Any]],
) -> list[list[Any]]:
    output: list[list[Any]] = []
    for record in records:
        attributes = record.get("attributes", {})
        if not attributes:
            output.append(
                [
                    pdf_name,
                    record.get("source_page"),
                    record.get("source_table"),
                    record.get("product_code"),
                    record.get("code_position"),
                    "",
                    "",
                    ";".join(str(value) for value in record.get("source_rows", [])),
                    ";".join(str(value) for value in record.get("source_columns", [])),
                    record.get("method"),
                ]
            )
            continue
        for name, value in attributes.items():
            output.append(
                [
                    pdf_name,
                    record.get("source_page"),
                    record.get("source_table"),
                    record.get("product_code"),
                    record.get("code_position"),
                    name,
                    value,
                    ";".join(str(item) for item in record.get("source_rows", [])),
                    ";".join(str(item) for item in record.get("source_columns", [])),
                    record.get("method"),
                ]
            )
    return output


def structure_needs_vision(columns: Sequence[str], rows: Sequence[Sequence[str]]) -> bool:
    """Flag columns whose meaning is likely carried by swatches or icons."""
    visual_terms = ("colour", "color", "swatch", "icon", "symbol", "finish")
    for column_index, header in enumerate(columns):
        lowered = header.casefold()
        if "colour" in lowered or "color" in lowered:
            return True
        if not any(term in lowered for term in visual_terms):
            continue
        values = [
            row[column_index]
            for row in rows
            if column_index < len(row)
        ]
        if values and sum(not value for value in values) / len(values) >= 0.35:
            return True
    return False


def parse_page_spec(spec: str | None, page_count: int) -> list[int]:
    """Parse a 1-based page expression into sorted, unique 0-based indexes.

    Supported examples: ``1``, ``1-5``, ``1,3,7-10``, ``-5`` and ``10-``.
    """
    if page_count <= 0:
        return []
    if not spec or spec.strip().lower() in {"all", "*"}:
        return list(range(page_count))

    selected: set[int] = set()
    for token in spec.split(","):
        token = token.strip()
        if not token:
            continue
        if "-" in token:
            left, right = token.split("-", 1)
            start = int(left) if left.strip() else 1
            end = int(right) if right.strip() else page_count
            if start > end:
                raise ValueError(f"Invalid descending page range: {token!r}")
            for number in range(start, end + 1):
                if 1 <= number <= page_count:
                    selected.add(number - 1)
                else:
                    raise ValueError(
                        f"Page {number} is outside the document range 1-{page_count}."
                    )
        else:
            number = int(token)
            if not 1 <= number <= page_count:
                raise ValueError(
                    f"Page {number} is outside the document range 1-{page_count}."
                )
            selected.add(number - 1)

    if not selected:
        raise ValueError("The page expression selected no pages.")
    return sorted(selected)


def normalize_bbox(value: Sequence[Any]) -> list[float]:
    if len(value) != 4:
        raise ValueError(f"Expected a four-value bounding box, received: {value!r}")
    return [float(value[0]), float(value[1]), float(value[2]), float(value[3])]


def table_bbox(box: dict[str, Any], table: dict[str, Any]) -> list[float]:
    value = table.get("bbox")
    if isinstance(value, (list, tuple)) and len(value) == 4:
        return normalize_bbox(value)
    return [float(box[key]) for key in ("x0", "y0", "x1", "y1")]


def normalize_cells(value: Any, row_count: int, col_count: int) -> list[list[list[float] | None]]:
    """Return cells as a row/column matrix of bboxes or None."""
    if not isinstance(value, list):
        return [[None] * col_count for _ in range(row_count)]

    # Current PyMuPDF4LLM JSON uses a nested row -> column structure.
    if value and isinstance(value[0], list) and (
        not value[0] or value[0][0] is None or isinstance(value[0][0], (list, tuple))
    ):
        matrix: list[list[list[float] | None]] = []
        for row in value[:row_count]:
            out_row: list[list[float] | None] = []
            for cell in list(row)[:col_count]:
                if isinstance(cell, (list, tuple)) and len(cell) == 4:
                    out_row.append(normalize_bbox(cell))
                else:
                    out_row.append(None)
            out_row.extend([None] * (col_count - len(out_row)))
            matrix.append(out_row)
        matrix.extend([[None] * col_count for _ in range(row_count - len(matrix))])
        return matrix

    # Defensive support for a flat row-major cell list.
    flattened: list[list[float] | None] = []
    for cell in value:
        if isinstance(cell, (list, tuple)) and len(cell) == 4:
            flattened.append(normalize_bbox(cell))
        else:
            flattened.append(None)
    required = row_count * col_count
    flattened.extend([None] * max(0, required - len(flattened)))
    return [
        flattened[offset : offset + col_count]
        for offset in range(0, required, col_count)
    ]


# ---------------------------------------------------------------------------
# Deterministic table-boundary refinement
# ---------------------------------------------------------------------------


def _edge_column_is_sparse(
    rows: list[list[str]],
    column: int,
    *,
    min_fill: float,
    max_nonempty: int,
) -> bool:
    if not rows:
        return False
    values = [row[column].strip() for row in rows]
    nonempty = [value for value in values if value]
    if not nonempty:
        return True

    fill = len(nonempty) / len(rows)
    early_nonempty = [value.casefold() for value in values[: min(4, len(values))] if value]
    decorative_header = not early_nonempty or all(
        value in {"image", "picture", "photo", "illustration"}
        for value in early_nonempty
    )

    # A product-caption value in a false image column often duplicates a code in
    # the real table. This is exactly the pattern seen on many catalogue pages.
    duplicate_only = True
    for row, value in zip(rows, values):
        if not value:
            continue
        duplicates_in_row = sum(1 for candidate in row if candidate.strip() == value)
        duplicates_anywhere = sum(
            1 for candidate_row in rows for candidate in candidate_row if candidate.strip() == value
        )
        if duplicates_in_row < 2 and duplicates_anywhere < 2:
            duplicate_only = False
            break

    sparse = fill <= min_fill or len(nonempty) <= max_nonempty
    return sparse and (decorative_header or duplicate_only)


def bbox_from_cells(
    cells: Sequence[Sequence[Sequence[float] | None]],
    fallback: Sequence[float],
) -> list[float]:
    rectangles = [
        normalize_bbox(cell)
        for row in cells
        for cell in row
        if isinstance(cell, (list, tuple)) and len(cell) == 4
    ]
    if not rectangles:
        return normalize_bbox(fallback)
    return [
        min(rect[0] for rect in rectangles),
        min(rect[1] for rect in rectangles),
        max(rect[2] for rect in rectangles),
        max(rect[3] for rect in rectangles),
    ]


def refine_table(
    rows: Sequence[Sequence[Any]],
    cells: list[list[list[float] | None]],
    bbox: list[float],
    *,
    trim_sparse_edges: bool,
    min_column_fill: float,
    max_edge_nonempty: int,
    forced_drop_left: int = 0,
    forced_drop_right: int = 0,
    source: str = "pymupdf4llm_layout",
    source_strategy: str = "layout",
) -> RefinedTable:
    materialized = [list(row) for row in rows]
    raw_nulls = rectangularize_nulls(materialized)
    raw_rows = rectangularize(materialized)
    col_count = max((len(row) for row in raw_rows), default=0)
    raw_cells = normalize_cells(cells, len(raw_rows), col_count)

    left = 0
    right = col_count
    if trim_sparse_edges and col_count:
        while left < right - 1 and _edge_column_is_sparse(
            raw_rows,
            left,
            min_fill=min_column_fill,
            max_nonempty=max_edge_nonempty,
        ):
            left += 1
        while right > left + 1 and _edge_column_is_sparse(
            raw_rows,
            right - 1,
            min_fill=min_column_fill,
            max_nonempty=max_edge_nonempty,
        ):
            right -= 1

    left = min(right, left + max(0, forced_drop_left))
    right = max(left, right - max(0, forced_drop_right))

    refined_rows = [row[left:right] for row in raw_rows]
    refined_cells = [row[left:right] for row in raw_cells]
    refined_nulls = [row[left:right] for row in raw_nulls]
    refined_bbox = bbox_from_cells(refined_cells, bbox)

    return RefinedTable(
        raw_rows=raw_rows,
        refined_rows=refined_rows,
        raw_cells=raw_cells,
        refined_cells=refined_cells,
        raw_bbox=normalize_bbox(bbox),
        refined_bbox=refined_bbox,
        dropped_left=left,
        dropped_right=col_count - right,
        raw_nulls=raw_nulls,
        refined_nulls=refined_nulls,
        source=source,
        source_strategy=source_strategy,
    )


def refine_from_raw_edge_drops(
    table: RefinedTable,
    *,
    drop_left: int,
    drop_right: int,
) -> RefinedTable:
    """Rebuild a refined table from AI-reviewed drops on the RAW matrix.

    The AI is never allowed to invent coordinates. It may only choose how many
    complete raw edge columns to remove. The final crop is then recalculated
    from PyMuPDF4LLM's original cell bounding boxes.
    """
    col_count = len(table.raw_rows[0]) if table.raw_rows else 0
    left = max(0, int(drop_left))
    right = col_count - max(0, int(drop_right))
    if col_count == 0 or left >= right:
        raise ValueError(
            f"Invalid AI edge drops: left={left}, right_drop={drop_right}, "
            f"raw_columns={col_count}."
        )

    rows = [row[left:right] for row in table.raw_rows]
    cells = [row[left:right] for row in table.raw_cells]
    nulls = [row[left:right] for row in table.raw_nulls] if table.raw_nulls else []
    return RefinedTable(
        raw_rows=table.raw_rows,
        refined_rows=rows,
        raw_cells=table.raw_cells,
        refined_cells=cells,
        raw_bbox=table.raw_bbox,
        refined_bbox=bbox_from_cells(cells, table.raw_bbox),
        dropped_left=left,
        dropped_right=col_count - right,
        raw_nulls=table.raw_nulls,
        refined_nulls=nulls,
        source=table.source,
        source_strategy=table.source_strategy,
    )




def find_grid_candidates(page: pymupdf.Page) -> list[GridCandidate]:
    """Find line-grid tables that can act as a merged-cell geometry oracle."""
    candidates: list[GridCandidate] = []
    seen: set[tuple[int, int, int, int, int, int]] = set()
    for strategy in ("lines_strict",):
        try:
            finder = page.find_tables(strategy=strategy)
        except Exception as exc:
            LOGGER.debug("Grid strategy %s failed: %s", strategy, exc)
            continue
        for table in finder.tables:
            try:
                cells = [
                    [normalize_bbox(cell) if cell is not None else None for cell in row.cells]
                    for row in table.rows
                ]
                rows: list[list[Any]] = []
                for cell_row in cells:
                    value_row: list[Any] = []
                    for cell in cell_row:
                        if cell is None:
                            value_row.append(None)
                        else:
                            value_row.append(
                                clean_cell(page.get_text("text", clip=pymupdf.Rect(cell)))
                            )
                    rows.append(value_row)
                bbox = normalize_bbox(table.bbox)
            except Exception as exc:
                LOGGER.debug("Skipping malformed %s grid candidate: %s", strategy, exc)
                continue
            if not rows or not any(any(cell is not None for cell in row) for row in cells):
                continue
            key = (
                round(bbox[0]),
                round(bbox[1]),
                round(bbox[2]),
                round(bbox[3]),
                int(table.row_count),
                int(table.col_count),
            )
            if key in seen:
                continue
            seen.add(key)
            candidates.append(
                GridCandidate(
                    rows=rows,
                    cells=cells,
                    bbox=bbox,
                    strategy=strategy,
                    row_count=int(table.row_count),
                    col_count=int(table.col_count),
                )
            )
    return candidates


def count_merged_placeholders(table: RefinedTable) -> int:
    count = 0
    for row_index, row in enumerate(table.refined_rows):
        for column_index, value in enumerate(row):
            is_null = (
                row_index < len(table.refined_nulls)
                and column_index < len(table.refined_nulls[row_index])
                and table.refined_nulls[row_index][column_index]
            )
            if not is_null:
                continue
            # Count only placeholders that have a preceding non-empty origin in
            # the same row or column. This excludes unused blank image regions.
            left_value = next(
                (row[index] for index in range(column_index - 1, -1, -1) if row[index]),
                "",
            )
            above_value = next(
                (
                    table.refined_rows[index][column_index]
                    for index in range(row_index - 1, -1, -1)
                    if column_index < len(table.refined_rows[index])
                    and table.refined_rows[index][column_index]
                ),
                "",
            )
            if left_value or above_value:
                count += 1
    return count


def select_grid_refinement(
    candidates: Sequence[GridCandidate],
    layout_table: RefinedTable,
    *,
    trim_sparse_edges: bool,
    min_column_fill: float,
    max_edge_nonempty: int,
    forced_drop_left: int,
    forced_drop_right: int,
    minimum_score: float,
) -> tuple[RefinedTable | None, dict[str, Any]]:
    """Choose a compatible grid table only when it clearly improves row spans."""
    best: RefinedTable | None = None
    best_meta: dict[str, Any] = {
        "selected": False,
        "reason": "No compatible line-grid table with reliable merged placeholders.",
    }
    layout_rows = len(layout_table.refined_rows)
    layout_cols = len(layout_table.refined_rows[0]) if layout_table.refined_rows else 0

    for candidate in candidates:
        refined = refine_table(
            candidate.rows,
            candidate.cells,
            candidate.bbox,
            trim_sparse_edges=trim_sparse_edges,
            min_column_fill=min_column_fill,
            max_edge_nonempty=max_edge_nonempty,
            forced_drop_left=forced_drop_left,
            forced_drop_right=forced_drop_right,
            source="pymupdf_grid_hybrid",
            source_strategy=candidate.strategy,
        )
        candidate_rows = len(refined.refined_rows)
        candidate_cols = len(refined.refined_rows[0]) if refined.refined_rows else 0
        row_similarity = (
            min(layout_rows, candidate_rows) / max(layout_rows, candidate_rows)
            if layout_rows and candidate_rows
            else 0.0
        )
        col_similarity = (
            min(layout_cols, candidate_cols) / max(layout_cols, candidate_cols)
            if layout_cols and candidate_cols
            else 0.0
        )
        overlap = bbox_overlap_ratio(layout_table.raw_bbox, refined.raw_bbox)
        merged = count_merged_placeholders(refined)
        merged_bonus = min(0.10, merged / 100.0)
        score = 0.45 * overlap + 0.30 * row_similarity + 0.25 * col_similarity + merged_bonus

        candidate.score = score
        candidate.overlap = overlap
        candidate.row_similarity = row_similarity
        candidate.col_similarity = col_similarity
        candidate.merged_placeholders = merged

        valid = (
            score >= minimum_score
            and overlap >= 0.60
            and row_similarity >= 0.75
            and col_similarity >= 0.75
            and merged >= 2
        )
        LOGGER.debug(
            "Grid candidate %s score=%.3f overlap=%.3f rows=%.3f cols=%.3f merged=%s valid=%s",
            candidate.strategy,
            score,
            overlap,
            row_similarity,
            col_similarity,
            merged,
            valid,
        )
        if valid and (best is None or score > float(best_meta.get("score", 0.0))):
            best = refined
            best_meta = {
                "selected": True,
                "strategy": candidate.strategy,
                "score": score,
                "overlap": overlap,
                "row_similarity": row_similarity,
                "column_similarity": col_similarity,
                "merged_placeholders": merged,
                "layout_shape": [layout_rows, layout_cols],
                "grid_shape": [candidate_rows, candidate_cols],
            }

    return best, best_meta



def _row_bands_from_cells(table: RefinedTable) -> list[tuple[float, float]]:
    bands: list[tuple[float, float]] = []
    for row_index, row in enumerate(table.refined_cells):
        boxes = [cell for cell in row if cell is not None]
        if boxes:
            bands.append((min(cell[1] for cell in boxes), max(cell[3] for cell in boxes)))
        else:
            y0, y1 = table.refined_bbox[1], table.refined_bbox[3]
            height = (y1 - y0) / max(1, len(table.refined_rows))
            bands.append((y0 + row_index * height, y0 + (row_index + 1) * height))
    return bands


def _word_groups_for_row(words: Sequence[tuple[Any, ...]]) -> list[dict[str, Any]]:
    if not words:
        return []
    ordered = sorted(words, key=lambda item: (float(item[0]), float(item[1])))
    heights = [float(word[3]) - float(word[1]) for word in ordered]
    median_height = sorted(heights)[len(heights) // 2] if heights else 8.0
    gap_limit = max(7.0, min(18.0, median_height * 1.45))
    groups: list[list[tuple[Any, ...]]] = []
    current: list[tuple[Any, ...]] = []
    current_x1 = 0.0
    for word in ordered:
        x0 = float(word[0])
        if current and x0 - current_x1 > gap_limit:
            groups.append(current)
            current = []
        current.append(word)
        current_x1 = max(current_x1, float(word[2])) if len(current) > 1 else float(word[2])
    if current:
        groups.append(current)

    output: list[dict[str, Any]] = []
    for group in groups:
        x0 = min(float(word[0]) for word in group)
        y0 = min(float(word[1]) for word in group)
        x1 = max(float(word[2]) for word in group)
        y1 = max(float(word[3]) for word in group)
        # Preserve PDF reading order inside a cell.
        ordered_group = sorted(group, key=lambda item: (int(item[5]), int(item[6]), int(item[7])))
        text = " ".join(str(word[4]) for word in ordered_group)
        output.append(
            {
                "bbox": [x0, y0, x1, y1],
                "center": (x0 + x1) / 2.0,
                "text": clean_cell(text),
            }
        )
    return output


def repair_borderless_columns_from_words(
    page: pymupdf.Page,
    layout_table: RefinedTable,
    *,
    minimum_support: float = 0.50,
    max_columns: int = 20,
) -> tuple[RefinedTable | None, dict[str, Any]]:
    """Split layout columns using stable native-word x positions.

    This is a conservative fallback for borderless tables where PyMuPDF4LLM has
    merged two or more logical columns into one cell. It uses the existing row
    bands, so it does not redetect the whole table.
    """
    rows = layout_table.refined_rows
    current_columns = len(rows[0]) if rows else 0
    if len(rows) < 3 or current_columns < 1:
        return None, {"selected": False, "reason": "too_few_rows_or_columns"}

    multiline_cells = sum(
        1 for row in rows for value in row if "\n" in clean_cell(value)
    )
    if multiline_cells < 2:
        return None, {
            "selected": False,
            "reason": "no_repeated_multiline_cells_to_split",
            "multiline_cells": multiline_cells,
        }

    clip = pymupdf.Rect(layout_table.refined_bbox)
    clip.y0 = max(0.0, clip.y0 - 4.0)
    clip.y1 = min(float(page.rect.height), clip.y1 + 2.0)
    words = page.get_text("words", clip=clip, sort=True)
    bands = _row_bands_from_cells(layout_table)
    row_words: list[list[tuple[Any, ...]]] = [[] for _ in bands]
    for word in words:
        center_y = (float(word[1]) + float(word[3])) / 2.0
        distances = [
            abs(center_y - ((band[0] + band[1]) / 2.0))
            for band in bands
        ]
        row_index = min(range(len(bands)), key=lambda index: distances[index])
        band = bands[row_index]
        if band[0] - 4.0 <= center_y <= band[1] + 4.0:
            row_words[row_index].append(word)

    grouped_rows = [_word_groups_for_row(row) for row in row_words]
    counts = collections.Counter(
        len(groups) for groups in grouped_rows[1:] if groups
    )
    candidates = [
        (count, support)
        for count, support in counts.items()
        if current_columns < count <= max_columns
        and support >= max(2, round((len(rows) - 1) * minimum_support))
    ]
    if not candidates:
        return None, {
            "selected": False,
            "reason": "no_stable_larger_column_count",
            "current_columns": current_columns,
            "group_counts": dict(counts),
            "multiline_cells": multiline_cells,
        }

    # Prefer the count supported by the most data rows; break ties toward the
    # smaller count to avoid over-splitting.
    candidate_columns, support_count = max(
        candidates,
        key=lambda item: (item[1], -item[0]),
    )
    supporting = [
        groups for groups in grouped_rows[1:] if len(groups) == candidate_columns
    ]
    anchors: list[float] = []
    for column_index in range(candidate_columns):
        values = sorted(groups[column_index]["center"] for groups in supporting)
        anchors.append(values[len(values) // 2])

    anchor_gaps = [
        anchors[index + 1] - anchors[index]
        for index in range(len(anchors) - 1)
    ]
    if not anchor_gaps or min(anchor_gaps) < 12.0:
        return None, {
            "selected": False,
            "reason": "candidate_anchors_too_close",
            "anchors": anchors,
        }

    x0, y0, x1, y1 = layout_table.refined_bbox
    separators = [x0]
    separators.extend(
        (anchors[index] + anchors[index + 1]) / 2.0
        for index in range(len(anchors) - 1)
    )
    separators.append(x1)

    repaired_rows: list[list[str]] = []
    repaired_cells: list[list[list[float] | None]] = []
    filled_counts: list[int] = []
    for row_index, groups in enumerate(grouped_rows):
        values = [""] * candidate_columns
        for group in groups:
            nearest = min(
                range(candidate_columns),
                key=lambda index: abs(group["center"] - anchors[index]),
            )
            if values[nearest]:
                values[nearest] = clean_cell(values[nearest] + " " + group["text"])
            else:
                values[nearest] = group["text"]
        repaired_rows.append(values)
        band_y0, band_y1 = bands[row_index]
        repaired_cells.append(
            [
                [separators[index], band_y0, separators[index + 1], band_y1]
                for index in range(candidate_columns)
            ]
        )
        filled_counts.append(sum(bool(value) for value in values))

    data_support = sum(
        count >= max(2, candidate_columns - 1)
        for count in filled_counts[1:]
    ) / max(1, len(filled_counts) - 1)
    if data_support < minimum_support:
        return None, {
            "selected": False,
            "reason": "repaired_rows_not_consistently_populated",
            "data_support": data_support,
            "candidate_columns": candidate_columns,
        }

    repaired = RefinedTable(
        raw_rows=repaired_rows,
        refined_rows=repaired_rows,
        raw_cells=repaired_cells,
        refined_cells=repaired_cells,
        raw_bbox=list(layout_table.refined_bbox),
        refined_bbox=list(layout_table.refined_bbox),
        dropped_left=0,
        dropped_right=0,
        raw_nulls=[[False] * candidate_columns for _ in repaired_rows],
        refined_nulls=[[False] * candidate_columns for _ in repaired_rows],
        source="pymupdf_word_column_repair",
        source_strategy="native_word_x_clusters",
    )
    return repaired, {
        "selected": True,
        "reason": "stable_native_word_columns_split_merged_layout_cells",
        "layout_shape": [len(rows), current_columns],
        "word_shape": [len(repaired_rows), candidate_columns],
        "supporting_rows": support_count,
        "data_support": round(data_support, 3),
        "anchors": [round(value, 3) for value in anchors],
        "separators": [round(value, 3) for value in separators],
        "multiline_cells": multiline_cells,
    }


def boundary_review_reasons(table: RefinedTable, grid_meta: dict[str, Any]) -> list[str]:
    reasons: list[str] = []
    if grid_meta.get("selected"):
        return reasons
    rows = table.refined_rows
    if not rows:
        return ["empty_table"]
    width = len(rows[0])
    for index in (0, width - 1):
        values = [row[index] for row in rows if index < len(row)]
        fill = sum(bool(value) for value in values) / max(1, len(values))
        if fill < 0.15:
            reasons.append("sparse_refined_edge")
            break
    if table.dropped_left + table.dropped_right > 1:
        reasons.append("multiple_layout_edges_were_trimmed_without_grid_confirmation")
    return reasons


# ---------------------------------------------------------------------------
# Images and diagnostics
# ---------------------------------------------------------------------------


def page_to_image(page: pymupdf.Page, dpi: int) -> Image.Image:
    pix = page.get_pixmap(dpi=dpi, alpha=False)
    mode = "RGB" if pix.n < 4 else "RGBA"
    return Image.frombytes(mode, (pix.width, pix.height), pix.samples).convert("RGB")


def pdf_rect_to_pixels(rect: Sequence[float], dpi: int) -> tuple[int, int, int, int]:
    scale = dpi / 72.0
    x0, y0, x1, y1 = normalize_bbox(rect)
    return (
        round(x0 * scale),
        round(y0 * scale),
        round(x1 * scale),
        round(y1 * scale),
    )


def save_diagnostics(
    page_image: Image.Image,
    raw_bbox: Sequence[float],
    refined_bbox: Sequence[float],
    directory: Path,
    *,
    dpi: int,
    padding_points: float,
    name_prefix: str = "",
) -> tuple[Path, Path]:
    directory.mkdir(parents=True, exist_ok=True)

    overlay = page_image.copy()
    draw = ImageDraw.Draw(overlay)
    raw_px = pdf_rect_to_pixels(raw_bbox, dpi)
    refined_px = pdf_rect_to_pixels(refined_bbox, dpi)
    line_width = max(2, round(dpi / 72.0))
    draw.rectangle(raw_px, outline="red", width=line_width)
    draw.rectangle(refined_px, outline="green", width=line_width)
    draw.text((raw_px[0] + 4, raw_px[1] + 4), "RAW", fill="red")
    draw.text((refined_px[0] + 4, refined_px[1] + 18), "REFINED", fill="green")
    overlay_path = directory / f"{name_prefix}page_overlay.png"
    overlay.save(overlay_path)

    padding_px = round(padding_points * dpi / 72.0)
    crop_box = (
        max(0, refined_px[0] - padding_px),
        max(0, refined_px[1] - padding_px),
        min(page_image.width, refined_px[2] + padding_px),
        min(page_image.height, refined_px[3] + padding_px),
    )
    crop = page_image.crop(crop_box)
    crop_path = directory / f"{name_prefix}refined_crop.png"
    crop.save(crop_path)
    return overlay_path, crop_path


def _resize_for_ai(image: Image.Image, max_width: int) -> Image.Image:
    if max_width <= 0 or image.width <= max_width:
        return image
    ratio = max_width / image.width
    return image.resize(
        (max_width, max(1, round(image.height * ratio))),
        Image.Resampling.LANCZOS,
    )


def save_ai_inputs(
    page_image: Image.Image,
    raw_bbox: Sequence[float],
    refined_bbox: Sequence[float],
    directory: Path,
    *,
    dpi: int,
    max_width: int,
    name_prefix: str = "",
) -> tuple[Path, Path]:
    """Create compact local-AI images instead of sending the whole PDF page."""
    directory.mkdir(parents=True, exist_ok=True)
    raw_px = pdf_rect_to_pixels(raw_bbox, dpi)
    refined_px = pdf_rect_to_pixels(refined_bbox, dpi)
    context_padding = round(36 * dpi / 72.0)
    context_box = (
        max(0, min(raw_px[0], refined_px[0]) - context_padding),
        max(0, min(raw_px[1], refined_px[1]) - context_padding),
        min(page_image.width, max(raw_px[2], refined_px[2]) + context_padding),
        min(page_image.height, max(raw_px[3], refined_px[3]) + context_padding),
    )
    context = page_image.crop(context_box)
    draw = ImageDraw.Draw(context)
    line_width = max(2, round(dpi / 72.0))
    rx0, ry0, rx1, ry1 = raw_px
    gx0, gy0, gx1, gy1 = refined_px
    ox, oy = context_box[0], context_box[1]
    draw.rectangle((rx0 - ox, ry0 - oy, rx1 - ox, ry1 - oy), outline="red", width=line_width)
    draw.rectangle((gx0 - ox, gy0 - oy, gx1 - ox, gy1 - oy), outline="green", width=line_width)
    context = _resize_for_ai(context, max_width)
    context_path = directory / f"{name_prefix}ai_context.jpg"
    context.save(context_path, format="JPEG", quality=85, optimize=True)

    table_padding = round(2 * dpi / 72.0)
    table_box = (
        max(0, refined_px[0] - table_padding),
        max(0, refined_px[1] - table_padding),
        min(page_image.width, refined_px[2] + table_padding),
        min(page_image.height, refined_px[3] + table_padding),
    )
    crop = _resize_for_ai(page_image.crop(table_box), max_width)
    crop_path = directory / f"{name_prefix}ai_table.jpg"
    crop.save(crop_path, format="JPEG", quality=88, optimize=True)
    return context_path, crop_path


# ---------------------------------------------------------------------------
# Optional Ollama two-pass vision + structured-output stage
# ---------------------------------------------------------------------------


def load_target_schema(path: Path | None) -> list[TargetColumn]:
    if path is None:
        return []
    value = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(value, list):
        return [TargetColumn(str(item)) for item in value]
    if isinstance(value, dict) and isinstance(value.get("columns"), list):
        columns: list[TargetColumn] = []
        for item in value["columns"]:
            if isinstance(item, str):
                columns.append(TargetColumn(item))
            elif isinstance(item, dict) and item.get("name"):
                columns.append(
                    TargetColumn(str(item["name"]), str(item.get("description", "")))
                )
            else:
                raise ValueError(f"Invalid schema column: {item!r}")
        return columns
    raise ValueError("Schema must be a JSON list or an object containing a 'columns' list.")


def ai_review_response_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "table_present": {"type": "boolean"},
            "decision": {
                "type": "string",
                "enum": ["keep_deterministic", "adjust_raw_edges", "reject"],
            },
            "drop_left_columns": {"type": "integer", "minimum": 0},
            "drop_right_columns": {"type": "integer", "minimum": 0},
            "expected_data_columns": {"type": "integer", "minimum": 0},
            "expected_data_rows": {"type": "integer", "minimum": 0},
            "detected_header_rows": {"type": "integer", "minimum": 0},
            "issues": {"type": "array", "items": {"type": "string"}},
            "review_summary": {"type": "string"},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        },
        "required": [
            "table_present",
            "decision",
            "drop_left_columns",
            "drop_right_columns",
            "expected_data_columns",
            "expected_data_rows",
            "detected_header_rows",
            "issues",
            "review_summary",
            "confidence",
        ],
    }


def ai_structure_response_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "table_title": {"type": "string"},
            "columns": {"type": "array", "items": {"type": "string"}},
            "rows": {
                "type": "array",
                "items": {
                    "type": "array",
                    "items": {"type": "string"},
                },
            },
            "warnings": {"type": "array", "items": {"type": "string"}},
            "unresolved_cells": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "row_index": {"type": "integer", "minimum": 1},
                        "column": {"type": "string"},
                        "reason": {"type": "string"},
                    },
                    "required": ["row_index", "column", "reason"],
                },
            },
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        },
        "required": [
            "table_title",
            "columns",
            "rows",
            "warnings",
            "unresolved_cells",
            "confidence",
        ],
    }


def _image_base64(path: Path) -> str:
    return base64.b64encode(path.read_bytes()).decode("ascii")


def normalize_ollama_chat_url(value: str) -> str:
    url = value.rstrip("/")
    if url.endswith("/api/chat"):
        return url
    if url.endswith("/api"):
        return url + "/chat"
    return url + "/api/chat"


def ollama_chat_json(
    *,
    model: str,
    url: str,
    timeout: int,
    prompt: str,
    image_paths: Sequence[Path],
    response_schema: dict[str, Any],
    num_ctx: int,
    num_predict: int,
    keep_alive: str,
) -> dict[str, Any]:
    body = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": prompt,
                "images": [_image_base64(path) for path in image_paths],
            }
        ],
        "stream": False,
        "format": response_schema,
        "keep_alive": keep_alive,
        "options": {
            "temperature": 0,
            "num_ctx": num_ctx,
            "num_predict": num_predict,
        },
    }
    LOGGER.info(
        "Calling Ollama model %s with num_ctx=%s, num_predict=%s, "
        "%s image(s), and %s prompt character(s).",
        model,
        num_ctx,
        num_predict,
        len(image_paths),
        len(prompt),
    )
    request = urllib.request.Request(
        normalize_ollama_chat_url(url),
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        if exc.code == 400 and "exceeds the available context size" in detail:
            raise RuntimeError(
                "Ollama rejected the request because its context window was too "
                f"small. This request asked for num_ctx={num_ctx}. Increase "
                "--ollama-num-ctx (for example to 16384 or 24576), or reduce "
                "--render-dpi. Ollama response: "
                + detail
            ) from exc
        raise RuntimeError(f"Ollama returned HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(
            f"Could not reach Ollama at {normalize_ollama_chat_url(url)}: {exc}"
        ) from exc

    content = payload.get("message", {}).get("content")
    if not isinstance(content, str):
        raise RuntimeError(f"Unexpected Ollama response: {payload!r}")
    try:
        result = json.loads(content)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Ollama did not return valid JSON: {content}") from exc
    result["_ollama_model"] = model
    result["_ollama_usage"] = {
        "prompt_eval_count": payload.get("prompt_eval_count"),
        "eval_count": payload.get("eval_count"),
        "total_duration_ns": payload.get("total_duration"),
        "load_duration_ns": payload.get("load_duration"),
        "prompt_eval_duration_ns": payload.get("prompt_eval_duration"),
        "eval_duration_ns": payload.get("eval_duration"),
        "num_ctx_requested": num_ctx,
        "num_predict_requested": num_predict,
    }
    return result


def ollama_review_table(
    *,
    model: str,
    url: str,
    timeout: int,
    num_ctx: int,
    keep_alive: str,
    overlay_path: Path,
    crop_path: Path,
    page_number: int,
    table_number: int,
    raw_rows: list[list[str]],
    deterministic: RefinedTable,
    normalized_rows: list[list[str]],
) -> dict[str, Any]:
    raw_columns = len(raw_rows[0]) if raw_rows else 0
    prompt = f"""You are the FIRST-PASS quality reviewer for a product-catalogue
table extraction.

Image 1 is the full PDF page:
- RED is the original PyMuPDF4LLM table boundary.
- GREEN is the deterministic boundary after sparse edge-column removal.

Image 2 is the current GREEN crop.

Page: {page_number}
Table number on page: {table_number}
Raw column count: {raw_columns}
Deterministic raw-column drops:
- left: {deterministic.dropped_left}
- right: {deterministic.dropped_right}

RAW PyMuPDF4LLM matrix:
{json.dumps(raw_rows, ensure_ascii=False)}

DETERMINISTIC normalized matrix after reliable merged-cell expansion:
{json.dumps(normalized_rows, ensure_ascii=False)}

Your task is evaluation and boundary refinement, not final CSV formatting.

Rules:
1. Decide whether the red/green area contains a genuine product-data table.
2. Product photographs, standalone captions, page headings, logos, prose,
   footers, and decorative icons are not data columns.
3. drop_left_columns and drop_right_columns ALWAYS refer to complete columns in
   the RAW matrix, not the green matrix.
4. Only recommend edge-column removal. Never remove a genuine internal column.
5. Use decision="keep_deterministic" when the green result is already correct.
6. Use decision="adjust_raw_edges" only when a different raw edge slice is
   visibly better.
7. Use decision="reject" when this detected region is not a usable table.
8. expected_data_rows excludes header rows and section-label rows that do not
   themselves describe a product record.
9. Do not transcribe or structure the final records in this pass.
10. Lower confidence when the crop cuts through data, when headers are unclear,
    or when colour/icon-only values cannot be read reliably.
"""
    result = ollama_chat_json(
        model=model,
        url=url,
        timeout=timeout,
        prompt=prompt,
        image_paths=[overlay_path, crop_path],
        response_schema=ai_review_response_schema(),
        num_ctx=num_ctx,
        num_predict=1024,
        keep_alive=keep_alive,
    )

    # Defensive validation beyond the JSON schema.
    decision = str(result.get("decision", "reject"))
    if decision not in {"keep_deterministic", "adjust_raw_edges", "reject"}:
        raise RuntimeError(f"Unexpected AI review decision: {decision!r}")
    left = int(result.get("drop_left_columns", 0))
    right = int(result.get("drop_right_columns", 0))
    if left < 0 or right < 0 or (raw_columns and left + right >= raw_columns):
        raise RuntimeError(
            f"AI proposed invalid raw edge drops: left={left}, right={right}, "
            f"raw_columns={raw_columns}."
        )
    result["drop_left_columns"] = left
    result["drop_right_columns"] = right
    result["confidence"] = float(result.get("confidence", 0.0))
    return result


def ollama_structure_table(
    *,
    model: str,
    url: str,
    timeout: int,
    num_ctx: int,
    keep_alive: str,
    image_paths: Sequence[Path],
    page_number: int,
    table_number: int,
    reviewed_rows: list[list[str]],
    deterministic_columns: list[str],
    deterministic_rows: list[list[str]],
    review_result: dict[str, Any],
    target_columns: list[TargetColumn],
    known_product_codes: Sequence[str],
) -> dict[str, Any]:
    target_description = (
        "No fixed target schema was supplied. Infer table-specific columns. "
        "Different tables may legitimately have different columns."
    )
    if target_columns:
        target_description = (
            "Use these exact target columns in this exact order:\n"
            + "\n".join(
                f"- {column.name}: {column.description}" if column.description else f"- {column.name}"
                for column in target_columns
            )
        )

    visual_note = (
        "A reviewed table image is attached. Use it only to resolve visual-only "
        "values and ambiguous structure."
        if image_paths
        else "No image is attached. Work from the normalized selectable-text matrix."
    )
    prompt = f"""You are the final structuring stage for a product-catalogue table.

{visual_note}

Page: {page_number}
Table number on page: {table_number}

Boundary review or deterministic routing decision:
{json.dumps(review_result, ensure_ascii=False)}

Normalized extraction matrix. True PDF rowspans and colspans have already been
expanded into every row/column to which they visibly apply:
{json.dumps(reviewed_rows, ensure_ascii=False)}

Conservative deterministic CSV proposal:
Columns: {json.dumps(deterministic_columns, ensure_ascii=False)}
Rows: {json.dumps(deterministic_rows, ensure_ascii=False)}

Known product codes matched by the catalogue registry:
{json.dumps(list(known_product_codes), ensure_ascii=False)}

{target_description}

Produce a clean rectangular product dataset.

Rules:
1. Use only information visibly present in the reviewed table crop or reviewed
   matrix. Do not use outside knowledge.
2. Do not invent, calculate, expand abbreviations, or infer missing values.
3. Exclude header rows, section labels, prose, photographs, and captions from
   the output records.
4. Flatten multi-row headers into clear column names.
5. Preserve the already-expanded merged-cell values in every applicable product
   row. Do not turn them back into blanks.
6. Preserve product codes, symbols, units, punctuation, ranges, plus signs, and
   voltage/current notation exactly.
7. Every output row must contain exactly one string for every output column.
8. Use an empty string for an unreadable or genuinely blank value.
9. Record every uncertain or unreadable value in unresolved_cells.
10. Convert a colour swatch to a colour name only when visually unambiguous;
    otherwise leave the value empty and add an unresolved cell.
11. Confidence must reflect exact transcription and structural certainty.
12. Product codes may appear in the first column, last column, an internal
    matrix cell, several cells in one row, or as column headings. Treat the
    known-product-code list as an anchor, not as a schema.
13. When one visual row contains several product codes, emit one record per
    product code and repeat every shared row value. Preserve the code's source
    column meaning in a suitable field.
14. When product codes are column headings, treat left-hand row labels as
    attribute names and the values below each code as that product's values.
15. If no fixed target schema is supplied, keep the columns specific to this
    table rather than forcing a catalogue-wide schema.
"""
    result = ollama_chat_json(
        model=model,
        url=url,
        timeout=timeout,
        prompt=prompt,
        image_paths=list(image_paths),
        response_schema=ai_structure_response_schema(),
        num_ctx=num_ctx,
        num_predict=4096,
        keep_alive=keep_alive,
    )

    columns = [clean_cell(value) for value in result.get("columns", [])]
    rows = rectangularize(result.get("rows", []))

    if target_columns:
        expected = [column.name for column in target_columns]
        if columns != expected:
            LOGGER.warning(
                "AI columns %s did not exactly match target schema %s; attempting name-based reorder.",
                columns,
                expected,
            )
            index_by_name = {name.casefold(): index for index, name in enumerate(columns)}
            if all(name.casefold() in index_by_name for name in expected):
                rows = [
                    [row[index_by_name[name.casefold()]] for name in expected]
                    for row in rows
                ]
                columns = expected
            else:
                raise RuntimeError(
                    "AI output could not be aligned to the requested target schema."
                )

    if not columns:
        raise RuntimeError("AI returned no columns.")
    rows = [row[: len(columns)] + [""] * max(0, len(columns) - len(row)) for row in rows]

    result["columns"] = columns
    result["rows"] = rows
    result["confidence"] = float(result.get("confidence", 0.0))
    result["_ollama_model"] = model
    return result


def build_ai_quality_report(
    *,
    review_result: dict[str, Any] | None,
    structure_result: dict[str, Any] | None,
    review_threshold: float,
    structure_threshold: float,
    ai_applied_to_boundary: bool,
) -> dict[str, Any]:
    reasons: list[str] = []
    status = "accepted"

    if review_result is None:
        status = "not_run"
        reasons.append("AI review was not completed.")
    else:
        review_confidence = float(review_result.get("confidence", 0.0))
        if review_result.get("decision") == "reject":
            status = "rejected"
            reasons.append("The AI review classified the candidate as not being a usable table.")
        elif review_confidence < review_threshold:
            status = "needs_review"
            reasons.append(
                f"Boundary-review confidence {review_confidence:.2f} is below "
                f"the threshold {review_threshold:.2f}."
            )
        if review_result.get("issues"):
            reasons.extend(str(item) for item in review_result["issues"])

    if structure_result is not None:
        structure_confidence = float(structure_result.get("confidence", 0.0))
        if structure_confidence < structure_threshold and status != "rejected":
            status = "needs_review"
            reasons.append(
                f"Structuring confidence {structure_confidence:.2f} is below "
                f"the threshold {structure_threshold:.2f}."
            )
        if structure_result.get("warnings"):
            reasons.extend(str(item) for item in structure_result["warnings"])
        unresolved = structure_result.get("unresolved_cells") or []
        if unresolved and status == "accepted":
            status = "needs_review"
        if unresolved:
            reasons.append(f"{len(unresolved)} unresolved cell(s) were reported.")

    return {
        "status": status,
        "boundary_review_confidence": (
            float(review_result.get("confidence", 0.0)) if review_result else None
        ),
        "structure_confidence": (
            float(structure_result.get("confidence", 0.0)) if structure_result else None
        ),
        "boundary_review_threshold": review_threshold,
        "structure_threshold": structure_threshold,
        "ai_boundary_adjustment_applied": ai_applied_to_boundary,
        "reasons": reasons,
    }


# ---------------------------------------------------------------------------
# Overrides and output aggregation
# ---------------------------------------------------------------------------


def load_overrides(path: Path | None) -> dict[str, dict[str, Any]]:
    if path is None:
        return {}
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("Overrides JSON must be an object keyed by 'page:table'.")
    return {str(key): dict(item) for key, item in value.items() if isinstance(item, dict)}


def create_universal_long_rows(
    *,
    pdf_name: str,
    page_number: int,
    table_number: int,
    method: str,
    columns: Sequence[str],
    rows: Sequence[Sequence[str]],
) -> list[list[Any]]:
    output: list[list[Any]] = []
    for row_index, row in enumerate(rows, start=1):
        for column_index, value in enumerate(row, start=1):
            header = columns[column_index - 1] if column_index <= len(columns) else f"column_{column_index}"
            output.append(
                [
                    pdf_name,
                    page_number,
                    table_number,
                    method,
                    row_index,
                    column_index,
                    header,
                    value,
                ]
            )
    return output



def page_relationship(
    entry: dict[str, Any] | None,
    *,
    pdf_page: int,
) -> str:
    if not entry or not entry.get("canonical_registry"):
        return "unregistered"
    core = set(entry.get("expected_pdf_page_core", set()))
    window = set(entry.get("expected_pdf_pages", set()))
    if pdf_page in core:
        return "index_target_page"
    if pdf_page in window:
        return "near_index_target"
    if core or window:
        return "outside_index_window"
    return "no_index_page"


def _joined(values: Iterable[Any]) -> str:
    return ";".join(str(value) for value in sorted({str(v) for v in values if str(v).strip()}))


def write_registry_page_plan(
    output_root: Path,
    registry: dict[str, dict[str, Any]],
) -> None:
    rows: list[list[Any]] = [[
        "sku",
        "sku_normalized",
        "registry_status",
        "catalogue_pages_normalized",
        "expected_pdf_page_core",
        "expected_pdf_pages_with_radius",
        "index_source_pdf_pages",
        "pack_carton_raw",
        "pack_carton",
        "pallet_raw",
        "pallet",
    ]]
    for entry in sorted(registry.values(), key=lambda item: str(item["normalized_code"])):
        if not entry.get("canonical_registry"):
            continue
        rows.append([
            entry["code"],
            entry["normalized_code"],
            entry.get("registry_status", "confirmed"),
            _joined(entry.get("catalogue_pages_normalized", set())),
            _joined(entry.get("expected_pdf_page_core", set())),
            _joined(entry.get("expected_pdf_pages", set())),
            _joined(entry.get("source_pdf_pages", set())),
            _joined(entry.get("pack_carton_raw", set())),
            _joined(entry.get("pack_carton", set())),
            _joined(entry.get("pallet_raw", set())),
            _joined(entry.get("pallet", set())),
        ])
    write_csv(output_root / "sku_page_plan.csv", rows)


def build_catalogue_product_outputs(
    *,
    output_root: Path,
    pdf_name: str,
    catalogue_id: str,
    manufacturer: str,
    records: Sequence[dict[str, Any]],
    registry: dict[str, dict[str, Any]],
    selected_pdf_pages: Sequence[int],
    page_count: int,
) -> list[list[Any]]:
    """Write canonical products, attribute occurrences, conflicts and coverage.

    Returns additional rows for the editable review queue.
    """
    occurrence_rows: list[list[Any]] = [[
        "occurrence_id",
        "catalogue_id",
        "manufacturer",
        "source_pdf",
        "sku",
        "sku_normalized",
        "registry_status",
        "registry_match",
        "pdf_page",
        "table",
        "code_position",
        "source_rows",
        "source_columns",
        "page_relationship",
        "method",
    ]]
    attribute_rows: list[list[Any]] = [[
        "occurrence_id",
        "catalogue_id",
        "manufacturer",
        "sku",
        "sku_normalized",
        "source_attribute",
        "normalized_attribute",
        "attribute_value",
        "attribute_value_raw",
        "attribute_value_normalized",
        "unit",
        "datatype",
        "pdf_page",
        "table",
        "source_row",
        "source_column",
        "inherited",
        "page_relationship",
        "method",
    ]]
    review_rows: list[list[Any]] = []
    observed_by_key: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    attribute_values: dict[tuple[str, str], dict[str, list[str]]] = collections.defaultdict(
        lambda: collections.defaultdict(list)
    )
    unexpected_records: list[dict[str, Any]] = []

    for occurrence_number, record in enumerate(records, start=1):
        key = normalize_code_key(str(record.get("product_code", "")))
        if not key:
            continue
        entry = registry.get(key)
        relationship = page_relationship(
            entry,
            pdf_page=int(record.get("source_page", 0) or 0),
        )
        occurrence_id = f"{safe_slug(catalogue_id)}-{occurrence_number:07d}"
        record["_occurrence_id"] = occurrence_id
        record["_page_relationship"] = relationship
        observed_by_key[key].append(record)
        if not entry or not entry.get("canonical_registry"):
            unexpected_records.append(record)

        occurrence_rows.append([
            occurrence_id,
            catalogue_id,
            manufacturer,
            pdf_name,
            record.get("product_code", ""),
            key,
            entry.get("registry_status", "unregistered") if entry else "unregistered",
            record.get("registry_match", "exact_normalized"),
            record.get("source_page", ""),
            record.get("source_table", ""),
            record.get("code_position", ""),
            ";".join(str(value) for value in record.get("source_rows", [])),
            ";".join(str(value) for value in record.get("source_columns", [])),
            relationship,
            record.get("method", ""),
        ])
        if relationship == "outside_index_window":
            review_rows.append([
                pdf_name,
                record.get("source_page", ""),
                record.get("source_table", ""),
                record.get("product_code", ""),
                "sku_found_outside_index_window",
                (
                    f"Found on PDF page {record.get('source_page')}; expected "
                    f"pages {_joined(entry.get('expected_pdf_pages', set()) if entry else set())}"
                ),
                "",
                "",
                "",
                "open",
                "",
                "",
            ])

        observations = record.get("attribute_observations") or []
        if not observations:
            observations = [
                {
                    "source_attribute": name,
                    "normalized_attribute": normalize_attribute_name(name),
                    "value": value,
                    "source_row": None,
                    "source_column": None,
                    "inherited": False,
                }
                for name, value in record.get("attributes", {}).items()
            ]
        for observation in observations:
            source_attribute = clean_cell(observation.get("source_attribute", ""))
            normalized_attribute = clean_cell(
                observation.get("normalized_attribute", "")
            ) or normalize_attribute_name(source_attribute)
            value = clean_cell(observation.get("value", observation.get("value_raw", "")))
            value_raw = clean_cell(observation.get("value_raw", value)) or value
            value_normalized = clean_cell(observation.get("value_normalized", value)) or value
            unit = clean_cell(observation.get("unit", ""))
            datatype = clean_cell(observation.get("datatype", "text"))
            if not value:
                continue
            location = (
                f"p{record.get('source_page')}:t{record.get('source_table')}:"
                f"r{observation.get('source_row')}:c{observation.get('source_column')}"
            )
            if location not in attribute_values[(key, normalized_attribute)][value]:
                attribute_values[(key, normalized_attribute)][value].append(location)
            attribute_rows.append([
                occurrence_id,
                catalogue_id,
                manufacturer,
                record.get("product_code", ""),
                key,
                source_attribute,
                normalized_attribute,
                value,
                value_raw,
                value_normalized,
                unit,
                datatype,
                record.get("source_page", ""),
                record.get("source_table", ""),
                observation.get("source_row", ""),
                observation.get("source_column", ""),
                observation.get("inherited", False),
                relationship,
                record.get("method", ""),
            ])

    write_csv(output_root / "product_occurrences.csv", occurrence_rows)
    write_csv(output_root / "product_attributes.csv", attribute_rows)

    conflict_rows: list[list[Any]] = [[
        "sku",
        "sku_normalized",
        "normalized_attribute",
        "distinct_values",
        "value_locations",
        "review_status",
        "reviewer_value",
    ]]
    conflict_count_by_key: collections.Counter[str] = collections.Counter()
    for (key, attribute_name), value_locations in sorted(attribute_values.items()):
        if len(value_locations) <= 1:
            continue
        conflict_count_by_key[key] += 1
        display = registry.get(key, {}).get(
            "code",
            observed_by_key.get(key, [{}])[0].get("product_code", key),
        )
        conflict_rows.append([
            display,
            key,
            attribute_name,
            " || ".join(sorted(value_locations)),
            json.dumps(value_locations, ensure_ascii=False, sort_keys=True),
            "",
            "",
        ])
        review_rows.append([
            pdf_name,
            "",
            "",
            display,
            "attribute_conflict",
            f"{attribute_name}: {' || '.join(sorted(value_locations))}",
            "",
            "",
            "",
            "open",
            "",
            attribute_name,
        ])
    write_csv(output_root / "attribute_conflicts.csv", conflict_rows)

    all_canonical_keys = {
        key for key, entry in registry.items() if entry.get("canonical_registry")
    }
    selected_page_set = {int(page) for page in selected_pdf_pages}
    all_pages_selected = len(selected_page_set) >= page_count
    canonical_keys = {
        key
        for key in all_canonical_keys
        if all_pages_selected
        or not registry[key].get("catalogue_pages_normalized")
        or bool(set(registry[key].get("expected_pdf_pages", set())) & selected_page_set)
    }
    observed_keys = set(observed_by_key)
    all_keys = sorted(canonical_keys | observed_keys)
    products_rows: list[list[Any]] = [[
        "catalogue_id",
        "manufacturer",
        "sku",
        "sku_normalized",
        "registry_status",
        "extraction_status",
        "occurrence_count",
        "catalogue_pages_normalized",
        "expected_pdf_page_core",
        "expected_pdf_pages_with_radius",
        "found_pdf_pages",
        "pack_carton_raw",
        "pack_carton",
        "pallet_raw",
        "pallet",
        "conflict_count",
        "needs_review",
        "attributes_json",
    ]]
    for key in all_keys:
        entry = registry.get(key, {})
        observed = observed_by_key.get(key, [])
        display = entry.get(
            "code",
            observed[0].get("product_code", key) if observed else key,
        )
        by_attribute: dict[str, list[str]] = {}
        for (candidate_key, attribute_name), value_locations in attribute_values.items():
            if candidate_key == key:
                by_attribute[attribute_name] = sorted(value_locations)
        registry_status = entry.get("registry_status", "unregistered")
        needs_review = (
            registry_status == "needs_review"
            or key not in canonical_keys
            or conflict_count_by_key[key] > 0
            or any(
                record.get("_page_relationship") == "outside_index_window"
                for record in observed
            )
        )
        products_rows.append([
            catalogue_id,
            manufacturer,
            display,
            key,
            registry_status,
            "extracted" if observed else "not_extracted",
            len(observed),
            _joined(entry.get("catalogue_pages_normalized", set())),
            _joined(entry.get("expected_pdf_page_core", set())),
            _joined(entry.get("expected_pdf_pages", set())),
            _joined(record.get("source_page", "") for record in observed),
            _joined(entry.get("pack_carton_raw", set())),
            _joined(entry.get("pack_carton", set())),
            _joined(entry.get("pallet_raw", set())),
            _joined(entry.get("pallet", set())),
            conflict_count_by_key[key],
            "yes" if needs_review else "no",
            json.dumps(by_attribute, ensure_ascii=False, sort_keys=True),
        ])
        if registry_status == "needs_review":
            review_rows.append([
                pdf_name,
                "",
                "",
                display,
                "registry_needs_review",
                _joined(entry.get("review_reasons", set()) or entry.get("required_issues", set())),
                "",
                entry.get("confidence", ""),
                "",
                "open",
                "",
                "",
            ])

    write_csv(output_root / "products.csv", products_rows)
    coverage_rows: list[list[Any]] = [[
        "sku",
        "sku_normalized",
        "registry_status",
        "catalogue_pages_normalized",
        "expected_pdf_pages_with_radius",
        "extraction_status",
        "found_pdf_pages",
        "occurrence_count",
        "conflict_count",
        "needs_review",
    ]]
    for row in products_rows[1:]:
        coverage_rows.append([
            row[2],
            row[3],
            row[4],
            row[7],
            row[9],
            row[5],
            row[10],
            row[6],
            row[15],
            row[16],
        ])
    write_csv(output_root / "product_code_coverage.csv", coverage_rows)

    unexpected_rows: list[list[Any]] = [[
        "sku",
        "sku_normalized",
        "pdf_page",
        "table",
        "code_position",
        "method",
        "review_status",
        "reviewer_value",
    ]]
    for record in unexpected_records:
        unexpected_rows.append([
            record.get("product_code", ""),
            normalize_code_key(str(record.get("product_code", ""))),
            record.get("source_page", ""),
            record.get("source_table", ""),
            record.get("code_position", ""),
            record.get("method", ""),
            "open",
            "",
        ])
        review_rows.append([
            pdf_name,
            record.get("source_page", ""),
            record.get("source_table", ""),
            record.get("product_code", ""),
            "unexpected_pdf_sku",
            record.get("code_position", ""),
            "",
            "",
            "",
            "open",
            "",
            "",
        ])
    write_csv(output_root / "unexpected_pdf_skus.csv", unexpected_rows)

    unmatched_rows: list[list[Any]] = [[
        "sku",
        "sku_normalized",
        "registry_status",
        "catalogue_pages_normalized",
        "expected_pdf_pages_with_radius",
        "pack_carton",
        "pallet",
        "review_status",
        "reviewer_value",
    ]]
    for key in sorted(canonical_keys - observed_keys):
        entry = registry[key]
        unmatched_rows.append([
            entry["code"],
            key,
            entry.get("registry_status", "confirmed"),
            _joined(entry.get("catalogue_pages_normalized", set())),
            _joined(entry.get("expected_pdf_pages", set())),
            _joined(entry.get("pack_carton", set())),
            _joined(entry.get("pallet", set())),
            "open",
            "",
        ])
    write_csv(output_root / "unmatched_registry_skus.csv", unmatched_rows)

    # Fuzzy matching is advisory only.
    fuzzy_rows: list[list[Any]] = [[
        "unexpected_sku",
        "unexpected_normalized",
        "candidate_registry_sku",
        "candidate_normalized",
        "similarity",
        "pdf_page",
        "candidate_expected_pdf_pages",
        "page_support",
        "decision",
    ]]
    canonical_key_list = sorted(all_canonical_keys)
    for record in unexpected_records:
        unexpected = normalize_code_key(str(record.get("product_code", "")))
        if not unexpected:
            continue
        candidate_keys = difflib.get_close_matches(
            unexpected,
            canonical_key_list,
            n=3,
            cutoff=0.78,
        )
        for candidate_key in candidate_keys:
            entry = registry[candidate_key]
            similarity = difflib.SequenceMatcher(
                None, unexpected, candidate_key
            ).ratio()
            source_page = int(record.get("source_page", 0) or 0)
            expected_pages = set(entry.get("expected_pdf_pages", set()))
            fuzzy_rows.append([
                record.get("product_code", ""),
                unexpected,
                entry["code"],
                candidate_key,
                round(similarity, 4),
                source_page,
                _joined(expected_pages),
                "yes" if source_page in expected_pages else "no",
                "review_only",
            ])
    write_csv(output_root / "fuzzy_match_candidates.csv", fuzzy_rows)

    return review_rows


# ---------------------------------------------------------------------------
# Main extraction workflow
# ---------------------------------------------------------------------------


def extract_catalogue(args: argparse.Namespace) -> int:
    pdf_path = args.pdf.resolve()
    output_root = args.output.resolve()
    output_root.mkdir(parents=True, exist_ok=True)

    target_columns = load_target_schema(args.schema)
    table_profile = dtm.load_profile(args.table_profile)
    overrides = load_overrides(args.overrides)
    external_code_registry = load_external_product_codes(
        args.product_code_index,
        column_name=args.product_code_column,
    )
    for entry in external_code_registry.values():
        entry["canonical_registry"] = True
        entry["registry_status"] = "confirmed"
        _ensure_registry_metadata(entry)
    catalogue_registry = load_catalogue_sku_files(
        args.sku_registry,
        args.sku_index_rows,
    )
    canonical_registry = merge_code_registries(
        catalogue_registry,
        external_code_registry,
    )
    product_code_pattern = (
        re.compile(args.product_code_regex, re.IGNORECASE)
        if args.product_code_regex
        else DEFAULT_PRODUCT_CODE_PATTERN
    )

    with pymupdf.open(pdf_path) as doc:
        inferred_offset = infer_catalogue_page_offset(doc)
        if str(args.catalogue_page_offset).casefold() == "auto":
            page_offset = int(inferred_offset["offset"])
        else:
            try:
                page_offset = int(args.catalogue_page_offset)
            except ValueError as exc:
                raise ValueError(
                    "--catalogue-page-offset must be 'auto' or an integer."
                ) from exc
        apply_expected_page_windows(
            canonical_registry,
            offset=page_offset,
            radius=args.index_page_radius,
            page_count=doc.page_count,
        )
        write_json(
            output_root / "catalogue_page_mapping.json",
            {
                "mode": "auto" if str(args.catalogue_page_offset).casefold() == "auto" else "manual",
                "selected_offset": page_offset,
                "formula": "pdf_page = catalogue_page_normalized + offset",
                "index_page_radius": args.index_page_radius,
                "automatic_inference": inferred_offset,
            },
        )
        write_registry_page_plan(output_root, canonical_registry)

        if args.review_queue_input:
            selected_pages = pages_from_review_queue(
                args.review_queue_input,
                page_count=doc.page_count,
            )
        elif str(args.pages).strip().casefold() == "registry":
            selected_pages = selected_pages_from_registry(
                canonical_registry,
                page_count=doc.page_count,
            )
        else:
            selected_pages = parse_page_spec(args.pages, doc.page_count)
        LOGGER.info(
            "Processing %s page(s) from %s. Printed-page offset=%s; index radius=%s.",
            len(selected_pages),
            pdf_path.name,
            page_offset,
            args.index_page_radius,
        )

        if args.code_registry_scope == "off":
            pdf_code_registry: dict[str, dict[str, Any]] = {}
        else:
            registry_pages = (
                selected_pages
                if args.code_registry_scope == "selected"
                else list(range(doc.page_count))
            )
            pdf_code_registry = discover_pdf_product_codes(
                doc,
                page_indexes=registry_pages,
            )
        product_code_registry = merge_code_registries(
            pdf_code_registry,
            canonical_registry,
        )
        registry_rows = serialise_code_registry(product_code_registry)
        registry_json_path = output_root / "product_code_registry.json"
        write_json(registry_json_path, registry_rows)
        product_code_registry_size = len(product_code_registry)
        write_csv(
            output_root / "product_code_registry.csv",
            [[
                "code",
                "normalized_code",
                "confidence",
                "registry_status",
                "canonical_registry",
                "sources",
                "pdf_discovery_pages",
                "catalogue_pages_normalized",
                "expected_pdf_page_core",
                "expected_pdf_pages",
                "index_source_pdf_pages",
                "occurrences",
                "pack_carton_raw",
                "pack_carton",
                "pallet_raw",
                "pallet",
                "required_issues",
                "optional_warnings",
            ]] + [
                [
                    item["code"],
                    item["normalized_code"],
                    item["confidence"],
                    item["registry_status"],
                    item["canonical_registry"],
                    ";".join(item["sources"]),
                    ";".join(str(page) for page in item["pages"]),
                    ";".join(str(page) for page in item["catalogue_pages_normalized"]),
                    ";".join(str(page) for page in item["expected_pdf_page_core"]),
                    ";".join(str(page) for page in item["expected_pdf_pages"]),
                    ";".join(str(page) for page in item["source_pdf_pages"]),
                    item["occurrences"],
                    ";".join(item["pack_carton_raw"]),
                    ";".join(item["pack_carton"]),
                    ";".join(item["pallet_raw"]),
                    ";".join(item["pallet"]),
                    ";".join(item["required_issues"]),
                    ";".join(item["optional_warnings"]),
                ]
                for item in registry_rows
            ],
        )
        LOGGER.info(
            "Product-code registry contains %s code(s): %s from PDF, %s from external files.",
            len(product_code_registry),
            len(pdf_code_registry),
            len(external_code_registry),
        )
        if args.registry_only:
            write_json(
                output_root / "registry_manifest.json",
                {
                    "source_pdf": str(pdf_path),
                    "scope": args.code_registry_scope,
                    "external_product_code_index": (
                        str(args.product_code_index.resolve())
                        if args.product_code_index
                        else None
                    ),
                    "product_code_count": len(product_code_registry),
                    "canonical_sku_count": sum(
                        1 for entry in product_code_registry.values()
                        if entry.get("canonical_registry")
                    ),
                    "sku_registry": str(args.sku_registry.resolve()) if args.sku_registry else None,
                    "sku_index_rows": str(args.sku_index_rows.resolve()) if args.sku_index_rows else None,
                    "catalogue_page_offset": page_offset,
                    "index_page_radius": args.index_page_radius,
                },
            )
            LOGGER.info("Registry-only mode finished. Outputs written to %s", output_root)
            return 0

        # Release duplicate registry representations before loading the layout
        # model. This materially reduces memory pressure for 10,000+ SKU files.
        del registry_rows
        del pdf_code_registry
        del external_code_registry
        del catalogue_registry
        del canonical_registry
        del product_code_registry
        gc.collect()

        pymupdf4llm.use_layout(True)
        layout_batches_dir = output_root / "layout_batches"
        layout_batches_dir.mkdir(parents=True, exist_ok=True)
        layout_batch_manifest: list[dict[str, Any]] = []
        combined_layout_pages: list[dict[str, Any]] = []

        manifest: dict[str, Any] = {
            "source_pdf": str(pdf_path),
            "selected_pages_1_based": [page + 1 for page in selected_pages],
            "layout_batch_size": args.layout_batch_size,
            "layout_process_mode": args.layout_process_mode,
            "layout_timeout": args.layout_timeout,
            "write_combined_layout_json": args.write_combined_layout_json,
            "pymupdf_version": getattr(pymupdf, "__version__", "unknown"),
            "pymupdf4llm_version": str(
                getattr(pymupdf4llm, "version", getattr(pymupdf4llm, "__version__", "unknown"))
            ),
            "hybrid_grid_enabled": not args.no_hybrid_grid,
            "word_column_repair_enabled": not args.no_word_column_repair,
            "merged_cell_expansion_enabled": not args.no_expand_merged_cells,
            "product_code_registry_scope": args.code_registry_scope,
            "product_code_registry_size": product_code_registry_size,
            "external_product_code_index": (
                str(args.product_code_index.resolve()) if args.product_code_index else None
            ),
            "sku_registry": str(args.sku_registry.resolve()) if args.sku_registry else None,
            "sku_index_rows": str(args.sku_index_rows.resolve()) if args.sku_index_rows else None,
            "catalogue_id": args.catalogue_id,
            "manufacturer": args.manufacturer,
            "table_profile": str(args.table_profile.resolve()) if args.table_profile else None,
            "dynamic_table_model": True,
            "continuation_joins_enabled": not args.no_continuation_joins,
            "continuation_auto_threshold": args.continuation_auto_threshold,
            "continuation_review_threshold": args.continuation_review_threshold,
            "normalization_priority": "attribute_precision",
            "catalogue_page_offset": page_offset,
            "index_page_radius": args.index_page_radius,
            "review_queue_input": (
                str(args.review_queue_input.resolve()) if args.review_queue_input else None
            ),
            "ai_mode": args.ai_mode,
            "ai_review_policy": args.ai_review_policy,
            "ai_structure_input": args.ai_structure_input,
            "ollama_model": args.ollama_model,
            "ollama_num_ctx": args.ollama_num_ctx,
            "ollama_keep_alive": args.ollama_keep_alive,
            "min_ai_review_confidence": args.min_ai_review_confidence,
            "min_ai_structure_confidence": args.min_ai_structure_confidence,
            "ai_low_confidence_action": args.ai_low_confidence_action,
            "tables": [],
        }
        long_rows: list[list[Any]] = [[
            "source_pdf",
            "page",
            "table",
            "method",
            "row_index",
            "column_index",
            "column_name",
            "value",
        ]]
        review_queue: list[list[Any]] = [[
            "source_pdf",
            "page",
            "table",
            "sku",
            "issue_type",
            "deterministic_result",
            "ai_suggestion",
            "confidence",
            "source_crop_path",
            "review_status",
            "reviewer_value",
            "attribute_name",
        ]]
        structured_tables: list[dict[str, Any]] = []
        all_product_records: list[dict[str, Any]] = []
        product_long_rows: list[list[Any]] = [[
            "source_pdf",
            "page",
            "table",
            "product_code",
            "code_position",
            "attribute_name",
            "attribute_value",
            "source_rows",
            "source_columns",
            "method",
        ]]

        for batch_number, batch_start in enumerate(
            range(0, len(selected_pages), args.layout_batch_size),
            start=1,
        ):
            batch_page_indexes = selected_pages[
                batch_start : batch_start + args.layout_batch_size
            ]
            batch_json_path = (
                layout_batches_dir
                / f"batch_{batch_number:05d}_pages_{batch_page_indexes[0] + 1:04d}"
                  f"-{batch_page_indexes[-1] + 1:04d}.json"
            )
            batch_pdf_path = batch_json_path.with_suffix(".pdf")

            def ensure_batch_pdf() -> None:
                if batch_pdf_path.exists():
                    return
                subset_doc = pymupdf.open()
                try:
                    for source_page_index in batch_page_indexes:
                        subset_doc.insert_pdf(
                            doc,
                            from_page=source_page_index,
                            to_page=source_page_index,
                        )
                    subset_doc.save(
                        batch_pdf_path,
                        garbage=4,
                        deflate=True,
                    )
                finally:
                    subset_doc.close()

            if args.resume and batch_json_path.exists():
                batch_layout = json.loads(
                    batch_json_path.read_text(encoding="utf-8")
                )
                LOGGER.info(
                    "Layout batch %s: reused %s.",
                    batch_number,
                    batch_json_path.name,
                )
            else:
                ensure_batch_pdf()
                LOGGER.info(
                    "Layout batch %s: analysing %s page(s), PDF pages %s-%s.",
                    batch_number,
                    len(batch_page_indexes),
                    batch_page_indexes[0] + 1,
                    batch_page_indexes[-1] + 1,
                )
                if args.layout_process_mode == "subprocess":
                    try:
                        worker_error_path = batch_json_path.with_suffix(".error.txt")
                        completed = subprocess.run(
                            [
                                sys.executable,
                                "-c",
                                LAYOUT_WORKER_CODE,
                                str(batch_pdf_path),
                                str(batch_json_path),
                                "1" if args.use_ocr else "0",
                                str(worker_error_path),
                            ],
                            check=False,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            timeout=args.layout_timeout,
                        )
                    except subprocess.TimeoutExpired as exc:
                        raise RuntimeError(
                            f"PyMuPDF4LLM layout batch {batch_number} exceeded "
                            f"{args.layout_timeout} seconds."
                        ) from exc
                    if completed.returncode != 0:
                        detail = ""
                        if worker_error_path.exists():
                            detail = worker_error_path.read_text(
                                encoding="utf-8", errors="replace"
                            ).strip()
                        raise RuntimeError(
                            "PyMuPDF4LLM layout worker failed for batch "
                            f"{batch_number}: {detail or 'no error text was returned'}"
                        )
                    if worker_error_path.exists():
                        worker_error_path.unlink()
                    batch_json_text = batch_json_path.read_text(encoding="utf-8")
                    batch_layout = json.loads(batch_json_text)
                else:
                    batch_json_text = pymupdf4llm.to_json(
                        str(batch_pdf_path),
                        pages=list(range(len(batch_page_indexes))),
                        use_ocr=args.use_ocr,
                    )
                    batch_layout = json.loads(batch_json_text)
                    write_json(batch_json_path, batch_layout)
                LOGGER.info(
                    "Layout batch %s: PyMuPDF4LLM returned %s characters.",
                    batch_number,
                    len(batch_json_text),
                )
                LOGGER.info(
                    "Layout batch %s: wrote %s.",
                    batch_number,
                    batch_json_path.name,
                )


            # Load the registry only after the memory-heavy layout stage.
            # It is released again before the next batch.
            product_code_registry = load_serialised_code_registry(
                registry_json_path
            )
            ensure_batch_pdf()
            batch_processing_doc = pymupdf.open(batch_pdf_path)
            batch_pages = batch_layout.get("pages", [])
            layout_batch_manifest.append({
                "batch_number": batch_number,
                "pdf_pages": [page + 1 for page in batch_page_indexes],
                "layout_json": str(batch_json_path.relative_to(output_root)),
                "page_count": len(batch_pages),
            })
            for local_page_index, page_data in enumerate(batch_pages):
                if local_page_index >= len(batch_page_indexes):
                    LOGGER.warning(
                        "Layout batch %s returned more pages than requested.",
                        batch_number,
                    )
                    break
                page_index = batch_page_indexes[local_page_index]
                page_number = page_index + 1
                page_data["page_number"] = page_number
                combined_layout_pages.append(page_data)
                page = batch_processing_doc[local_page_index]
                LOGGER.info("Page %s: rendering diagnostic image.", page_number)
                page_image = page_to_image(page, args.render_dpi)
                LOGGER.info("Page %s: finding companion grid candidates.", page_number)
                grid_candidates = [] if args.no_hybrid_grid else find_grid_candidates(page)
                LOGGER.info("Page %s: grid candidate search returned %s.", page_number, len(grid_candidates))
                table_boxes = [
                    box
                    for box in page_data.get("boxes", [])
                    if box.get("boxclass") == "table" and isinstance(box.get("table"), dict)
                ]
                LOGGER.info("Page %s: %s table box(es).", page_number, len(table_boxes))

                for table_number, box in enumerate(table_boxes, start=1):
                    table = box["table"]
                    source_rows = table.get("extract") or []
                    if not source_rows:
                        LOGGER.warning("Skipping empty table on page %s, table %s.", page_number, table_number)
                        continue

                    table_dir = output_root / f"page_{page_number:04d}" / f"table_{table_number:02d}"
                    final_json_path = table_dir / "final.json"
                    if args.resume and final_json_path.exists():
                        try:
                            existing = json.loads(final_json_path.read_text(encoding="utf-8"))
                            columns = [clean_cell(value) for value in existing.get("columns", [])]
                            rows = rectangularize(existing.get("rows", []))
                            method = str(existing.get("method", "resumed"))
                            structured_tables.append({
                                "page": page_number,
                                "table": table_number,
                                "columns": columns,
                                "rows": rows,
                                "method": method,
                            })
                            resumed_product_records = dynamic_product_records(
                                columns,
                                rows,
                                registry=product_code_registry,
                                product_code_pattern=product_code_pattern,
                                page_number=page_number,
                                table_number=table_number,
                                method=method,
                            )
                            all_product_records.extend(resumed_product_records)
                            product_long_rows.extend(
                                product_records_long_rows(
                                    pdf_path.name,
                                    resumed_product_records,
                                )
                            )
                            long_rows.extend(
                                create_universal_long_rows(
                                    pdf_name=pdf_path.name,
                                    page_number=page_number,
                                    table_number=table_number,
                                    method=method,
                                    columns=columns,
                                    rows=rows,
                                )
                            )
                            manifest["tables"].append({
                                "page": page_number,
                                "table": table_number,
                                "directory": str(table_dir.relative_to(output_root)),
                                "final_method": method,
                                "resumed": True,
                            })
                            LOGGER.info("Page %s table %s: resumed existing final.json.", page_number, table_number)
                            continue
                        except Exception as exc:
                            LOGGER.warning("Could not resume %s: %s. Reprocessing.", final_json_path, exc)

                    bbox = table_bbox(box, table)
                    source_width = max((len(row) for row in source_rows), default=0)
                    source_cells = normalize_cells(table.get("cells"), len(source_rows), source_width)
                    override_key = f"{page_number}:{table_number}"
                    override = overrides.get(override_key, {})
                    forced_left = int(override.get("drop_left_columns", 0))
                    forced_right = int(override.get("drop_right_columns", 0))

                    layout_refined = refine_table(
                        source_rows,
                        source_cells,
                        bbox,
                        trim_sparse_edges=not args.no_trim_sparse_edges,
                        min_column_fill=args.min_column_fill,
                        max_edge_nonempty=args.max_edge_nonempty,
                        forced_drop_left=forced_left,
                        forced_drop_right=forced_right,
                        source="pymupdf4llm_layout",
                        source_strategy="layout",
                    )

                    grid_refined, grid_meta = select_grid_refinement(
                        grid_candidates,
                        layout_refined,
                        trim_sparse_edges=not args.no_trim_sparse_edges,
                        min_column_fill=args.min_column_fill,
                        max_edge_nonempty=args.max_edge_nonempty,
                        forced_drop_left=forced_left,
                        forced_drop_right=forced_right,
                        minimum_score=args.grid_min_score,
                    )
                    word_refined: RefinedTable | None = None
                    word_meta: dict[str, Any] = {
                        "selected": False,
                        "reason": "line_grid_selected_or_word_repair_disabled",
                    }
                    if grid_refined is None and not args.no_word_column_repair:
                        word_refined, word_meta = repair_borderless_columns_from_words(
                            page,
                            layout_refined,
                            minimum_support=args.word_column_min_support,
                        )
                    deterministic = grid_refined or word_refined or layout_refined
                    if isinstance(override.get("bbox"), list):
                        deterministic.refined_bbox = normalize_bbox(override["bbox"])

                    table_dir.mkdir(parents=True, exist_ok=True)
                    write_csv(table_dir / "layout_matrix.csv", layout_refined.refined_rows)
                    if grid_refined is not None:
                        write_csv(table_dir / "grid_matrix.csv", grid_refined.refined_rows)
                    if word_refined is not None:
                        write_csv(table_dir / "word_repaired_matrix.csv", word_refined.refined_rows)
                    write_csv(table_dir / "raw_matrix.csv", deterministic.raw_rows)
                    write_csv(table_dir / "refined_matrix.csv", deterministic.refined_rows)
                    write_json(table_dir / "pymupdf4llm_table.json", table)
                    write_json(table_dir / "03_raw_pymupdf4llm.json", table)

                    normalized_rows, expansion_events = (
                        expand_merged_values(deterministic)
                        if not args.no_expand_merged_cells
                        else ([list(row) for row in deterministic.refined_rows], [])
                    )
                    write_csv(table_dir / "normalized_matrix.csv", normalized_rows)
                    write_json(table_dir / "merged_cell_expansion.json", expansion_events)

                    deterministic_columns, deterministic_rows, format_meta = deterministic_records(
                        normalized_rows,
                        deterministic.refined_rows,
                        product_code_pattern=product_code_pattern,
                    )
                    write_csv(
                        table_dir / "deterministic_structured.csv",
                        [deterministic_columns, *deterministic_rows],
                    )
                    # Dynamic cell-graph interpretation.  This keeps the faithful
                    # table reconstruction separate from one-record-per-SKU
                    # normalization and does not require a catalogue-wide schema.
                    dynamic_analysis = dtm.analyse_table(
                        page=page,
                        page_number=page_number,
                        table_number=table_number,
                        bbox=deterministic.refined_bbox,
                        rows=normalized_rows,
                        cells=deterministic.refined_cells,
                        registry=product_code_registry,
                        profile=table_profile,
                    )
                    dtm.write_analysis_artifacts(table_dir, dynamic_analysis)
                    dynamic_columns = dynamic_analysis.reconstructed_columns
                    dynamic_rows = dynamic_analysis.reconstructed_rows
                    deterministic_product_records = dynamic_analysis.product_records
                    deterministic_product_columns, deterministic_product_rows = dtm.product_records_to_wide(
                        deterministic_product_records
                    )
                    write_json(
                        table_dir / "deterministic_product_records.json",
                        deterministic_product_records,
                    )
                    write_csv(
                        table_dir / "deterministic_product_records.csv",
                        [deterministic_product_columns, *deterministic_product_rows],
                    )
                    format_meta["dynamic_table_classification"] = dynamic_analysis.classification
                    format_meta["dynamic_validation"] = dynamic_analysis.validation

                    write_json(
                        table_dir / "geometry.json",
                        {
                            "deterministic_source": deterministic.source,
                            "deterministic_strategy": deterministic.source_strategy,
                            "grid_match": grid_meta,
                            "word_column_repair": word_meta,
                            "raw_bbox": deterministic.raw_bbox,
                            "refined_bbox": deterministic.refined_bbox,
                            "dropped_left_columns": deterministic.dropped_left,
                            "dropped_right_columns": deterministic.dropped_right,
                            "raw_cells": deterministic.raw_cells,
                            "refined_cells": deterministic.refined_cells,
                            "raw_nulls": deterministic.raw_nulls,
                            "refined_nulls": deterministic.refined_nulls,
                            "format": format_meta,
                            "dynamic_selected_bbox": dynamic_analysis.bbox,
                            "dynamic_matrix_selection": dynamic_analysis.extraction_selection,
                        },
                    )

                    overlay_path, crop_path = save_diagnostics(
                        page_image,
                        deterministic.raw_bbox,
                        dynamic_analysis.bbox,
                        table_dir,
                        dpi=args.render_dpi,
                        padding_points=args.crop_padding,
                    )
                    # Version 9 diagnostic contract. These names make it clear
                    # which stage should be inspected when an SKU-to-attribute
                    # relationship is wrong.
                    page_image.save(table_dir / "00_source_page.png")
                    shutil.copyfile(overlay_path, table_dir / "01_table_overlay.png")
                    shutil.copyfile(crop_path, table_dir / "02_table_crop.png")

                    final_method = f"{deterministic.source}_dynamic_cell_graph"
                    final_columns = dynamic_columns
                    final_rows = dynamic_rows
                    review_result: dict[str, Any] | None = None
                    structure_result: dict[str, Any] | None = None
                    ai_error: str | None = None
                    ai_boundary_applied = False
                    reviewed_table = deterministic
                    reviewed_rows = normalized_rows
                    reviewed_columns = dynamic_columns
                    reviewed_records = dynamic_rows
                    effective_geometry_meta = (
                        grid_meta if grid_meta.get("selected") else word_meta
                    )
                    review_reasons = boundary_review_reasons(
                        deterministic,
                        effective_geometry_meta,
                    )
                    dynamic_confidence = float(dynamic_analysis.classification.get("confidence", 0.0))
                    if dynamic_confidence < 0.90:
                        review_reasons.append(
                            "dynamic_table_classification_below_auto_accept:"
                            f"{dynamic_analysis.classification.get('selected')}:{dynamic_confidence:.2f}"
                        )
                    if review_reasons:
                        review_queue.append([
                            pdf_path.name,
                            page_number,
                            table_number,
                            "",
                            "table_geometry",
                            f"{deterministic.source}: {'; '.join(review_reasons)}",
                            "",
                            "",
                            str(crop_path.relative_to(output_root)),
                            "open",
                            "",
                            "",
                        ])

                    should_review = bool(args.ollama_model) and (
                        args.ai_review_policy == "always"
                        or (args.ai_review_policy == "auto" and bool(review_reasons))
                    )
                    ai_context_path: Path | None = None
                    ai_crop_path: Path | None = None

                    try:
                        if should_review:
                            ai_context_path, ai_crop_path = save_ai_inputs(
                                page_image,
                                deterministic.raw_bbox,
                                deterministic.refined_bbox,
                                table_dir,
                                dpi=args.render_dpi,
                                max_width=args.ai_max_image_width,
                            )
                            review_result = ollama_review_table(
                                model=args.ollama_model,
                                url=args.ollama_url,
                                timeout=args.ollama_timeout,
                                num_ctx=args.ollama_num_ctx,
                                keep_alive=args.ollama_keep_alive,
                                overlay_path=ai_context_path,
                                crop_path=ai_crop_path,
                                page_number=page_number,
                                table_number=table_number,
                                raw_rows=deterministic.raw_rows,
                                deterministic=deterministic,
                                normalized_rows=normalized_rows,
                            )
                            write_json(table_dir / "ai_boundary_review.json", review_result)

                            review_confidence = float(review_result.get("confidence", 0.0))
                            if (
                                review_result.get("decision") == "adjust_raw_edges"
                                and review_confidence >= args.min_ai_review_confidence
                            ):
                                reviewed_table = refine_from_raw_edge_drops(
                                    deterministic,
                                    drop_left=int(review_result["drop_left_columns"]),
                                    drop_right=int(review_result["drop_right_columns"]),
                                )
                                ai_boundary_applied = (
                                    reviewed_table.dropped_left != deterministic.dropped_left
                                    or reviewed_table.dropped_right != deterministic.dropped_right
                                )
                                reviewed_rows, ai_expansion_events = (
                                    expand_merged_values(reviewed_table)
                                    if not args.no_expand_merged_cells
                                    else ([list(row) for row in reviewed_table.refined_rows], [])
                                )
                                reviewed_columns, reviewed_records, ai_format_meta = deterministic_records(
                                    reviewed_rows,
                                    reviewed_table.refined_rows,
                                    product_code_pattern=product_code_pattern,
                                )
                                write_csv(table_dir / "ai_refined_matrix.csv", reviewed_table.refined_rows)
                                write_csv(table_dir / "ai_normalized_matrix.csv", reviewed_rows)
                                write_csv(
                                    table_dir / "ai_deterministic_structured.csv",
                                    [reviewed_columns, *reviewed_records],
                                )
                                write_json(table_dir / "ai_merged_cell_expansion.json", ai_expansion_events)
                                write_json(table_dir / "ai_format.json", ai_format_meta)
                                save_diagnostics(
                                    page_image,
                                    deterministic.raw_bbox,
                                    reviewed_table.refined_bbox,
                                    table_dir,
                                    dpi=args.render_dpi,
                                    padding_points=args.crop_padding,
                                    name_prefix="ai_",
                                )
                                ai_context_path, ai_crop_path = save_ai_inputs(
                                    page_image,
                                    deterministic.raw_bbox,
                                    reviewed_table.refined_bbox,
                                    table_dir,
                                    dpi=args.render_dpi,
                                    max_width=args.ai_max_image_width,
                                    name_prefix="reviewed_",
                                )
                        elif args.ollama_model:
                            review_result = {
                                "table_present": True,
                                "decision": "keep_deterministic",
                                "drop_left_columns": deterministic.dropped_left,
                                "drop_right_columns": deterministic.dropped_right,
                                "expected_data_columns": len(reviewed_columns),
                                "expected_data_rows": len(reviewed_records),
                                "detected_header_rows": format_meta.get("header_rows", 0),
                                "issues": [],
                                "review_summary": "AI boundary review skipped by routing policy; deterministic geometry accepted.",
                                "confidence": 1.0,
                                "_skipped_by_routing": True,
                            }
                            write_json(table_dir / "ai_routing.json", review_result)

                        if args.ollama_model and args.ai_mode == "structure" and (
                            not review_result or review_result.get("decision") != "reject"
                        ):
                            input_mode = args.ai_structure_input
                            if input_mode == "auto":
                                input_mode = (
                                    "crop"
                                    if structure_needs_vision(reviewed_columns, reviewed_records)
                                    else "text"
                                )
                            structure_images: list[Path] = []
                            if input_mode == "crop":
                                if ai_crop_path is None:
                                    _, ai_crop_path = save_ai_inputs(
                                        page_image,
                                        deterministic.raw_bbox,
                                        reviewed_table.refined_bbox,
                                        table_dir,
                                        dpi=args.render_dpi,
                                        max_width=args.ai_max_image_width,
                                        name_prefix="structure_",
                                    )
                                structure_images = [ai_crop_path]

                            structure_result = ollama_structure_table(
                                model=args.ollama_model,
                                url=args.ollama_url,
                                timeout=args.ollama_timeout,
                                num_ctx=args.ollama_num_ctx,
                                keep_alive=args.ollama_keep_alive,
                                image_paths=structure_images,
                                page_number=page_number,
                                table_number=table_number,
                                reviewed_rows=reviewed_rows,
                                deterministic_columns=reviewed_columns,
                                deterministic_rows=reviewed_records,
                                review_result=review_result or {},
                                target_columns=target_columns,
                                known_product_codes=sorted({
                                    record["product_code"]
                                    for record in dynamic_product_records(
                                        reviewed_columns,
                                        reviewed_records,
                                        registry=product_code_registry,
                                        product_code_pattern=product_code_pattern,
                                        page_number=page_number,
                                        table_number=table_number,
                                        method="ai_prompt_registry_match",
                                    )
                                }),
                            )
                            structure_result["_input_mode"] = input_mode
                            write_json(table_dir / "ai_structured.json", structure_result)
                            write_csv(
                                table_dir / "ai_structured.csv",
                                [structure_result["columns"], *structure_result["rows"]],
                            )

                            review_ok = (
                                review_result is None
                                or review_result.get("_skipped_by_routing")
                                or float(review_result.get("confidence", 0.0))
                                >= args.min_ai_review_confidence
                            )
                            structure_ok = (
                                float(structure_result.get("confidence", 0.0))
                                >= args.min_ai_structure_confidence
                            )
                            ai_acceptable = bool(review_ok and structure_ok)
                            if ai_acceptable or args.ai_low_confidence_action == "use-ai":
                                final_method = f"ollama_structured_{input_mode}"
                                final_columns = structure_result["columns"]
                                final_rows = structure_result["rows"]
                            elif args.ai_low_confidence_action == "fail":
                                raise RuntimeError(
                                    "AI output did not meet the configured confidence thresholds."
                                )
                            else:
                                LOGGER.warning(
                                    "Keeping deterministic final.csv because AI confidence did not meet thresholds."
                                )

                    except Exception as exc:
                        ai_error = str(exc)
                        LOGGER.error("AI failed on page %s table %s: %s", page_number, table_number, exc)
                        write_json(table_dir / "ai_error.json", {"error": ai_error})
                        if args.fail_on_ai_error:
                            raise

                    if not args.ollama_model:
                        quality_report = {
                            "status": "deterministic",
                            "deterministic_source": deterministic.source,
                            "grid_match": grid_meta,
                            "word_column_repair": word_meta,
                            "merged_cells_expanded": len(expansion_events),
                            "review_reasons": review_reasons,
                        }
                    else:
                        quality_report = build_ai_quality_report(
                            review_result=review_result,
                            structure_result=structure_result,
                            review_threshold=args.min_ai_review_confidence,
                            structure_threshold=args.min_ai_structure_confidence,
                            ai_applied_to_boundary=ai_boundary_applied,
                        )
                        quality_report.update({
                            "deterministic_source": deterministic.source,
                            "grid_match": grid_meta,
                            "word_column_repair": word_meta,
                            "merged_cells_expanded": len(expansion_events),
                            "review_reasons": review_reasons,
                            "ai_review_skipped_by_routing": bool(
                                review_result and review_result.get("_skipped_by_routing")
                            ),
                            "ai_structure_input": (
                                structure_result.get("_input_mode") if structure_result else None
                            ),
                        })
                    write_json(table_dir / "quality_report.json", quality_report)

                    write_csv(table_dir / "final.csv", [final_columns, *final_rows])
                    if final_method.startswith("ollama_structured_"):
                        final_product_records = dynamic_product_records(
                            final_columns,
                            final_rows,
                            registry=product_code_registry,
                            product_code_pattern=product_code_pattern,
                            page_number=page_number,
                            table_number=table_number,
                            method=final_method,
                        )
                    else:
                        final_product_records = dynamic_analysis.product_records
                    product_columns, product_rows = dtm.product_records_to_wide(final_product_records)
                    # Keep the table model synchronized with the accepted
                    # product records so continuation joins can operate after
                    # all adjacent pages have been analysed.
                    table_model_path = table_dir / "table_model.json"
                    if table_model_path.exists():
                        table_model_payload = json.loads(table_model_path.read_text(encoding="utf-8"))
                        table_model_payload["product_records"] = final_product_records
                        table_model_payload["accepted_table_method"] = final_method
                        write_json(table_model_path, table_model_payload)
                    write_json(
                        table_dir / "product_records.json",
                        final_product_records,
                    )
                    write_csv(
                        table_dir / "product_records.csv",
                        [product_columns, *product_rows],
                    )

                    write_json(
                        final_json_path,
                        {
                            "columns": final_columns,
                            "rows": final_rows,
                            "method": final_method,
                            "quality_status": quality_report["status"],
                            "boundary_review_confidence": quality_report.get("boundary_review_confidence"),
                            "structure_confidence": quality_report.get("structure_confidence"),
                            "product_records": final_product_records,
                        },
                    )
                    all_product_records.extend(final_product_records)
                    product_long_rows.extend(
                        product_records_long_rows(
                            pdf_path.name,
                            final_product_records,
                        )
                    )
                    long_rows.extend(
                        create_universal_long_rows(
                            pdf_name=pdf_path.name,
                            page_number=page_number,
                            table_number=table_number,
                            method=final_method,
                            columns=final_columns,
                            rows=final_rows,
                        )
                    )
                    structured_tables.append({
                        "page": page_number,
                        "table": table_number,
                        "columns": final_columns,
                        "rows": final_rows,
                        "method": final_method,
                    })
                    manifest["tables"].append({
                        "page": page_number,
                        "table": table_number,
                        "directory": str(table_dir.relative_to(output_root)),
                        "deterministic_source": deterministic.source,
                        "deterministic_strategy": deterministic.source_strategy,
                        "grid_match": grid_meta,
                        "word_column_repair": word_meta,
                        "raw_bbox": deterministic.raw_bbox,
                        "refined_bbox": deterministic.refined_bbox,
                        "raw_shape": [
                            len(deterministic.raw_rows),
                            len(deterministic.raw_rows[0]) if deterministic.raw_rows else 0,
                        ],
                        "refined_shape": [
                            len(deterministic.refined_rows),
                            len(deterministic.refined_rows[0]) if deterministic.refined_rows else 0,
                        ],
                        "merged_cell_expansions": len(expansion_events),
                        "format": format_meta,
                        "dynamic_orientation": dynamic_analysis.classification.get("selected"),
                        "dynamic_orientation_confidence": dynamic_analysis.classification.get("confidence"),
                        "dynamic_normalization_status": dynamic_analysis.validation.get("normalization_status"),
                        "dropped_left_columns": deterministic.dropped_left,
                        "dropped_right_columns": deterministic.dropped_right,
                        "final_method": final_method,
                        "ai_error": ai_error,
                        "ai_review_decision": review_result.get("decision") if review_result else None,
                        "ai_boundary_adjustment_applied": ai_boundary_applied,
                        "quality_status": quality_report["status"],
                        "boundary_review_confidence": quality_report.get("boundary_review_confidence"),
                        "structure_confidence": quality_report.get("structure_confidence"),
                    })
                    # Checkpoint after every table so a long catalogue run can be resumed.
                    write_json(output_root / "manifest.json", manifest)

            batch_processing_doc.close()
            del product_code_registry
            gc.collect()
            if not args.keep_layout_batch_pdfs:
                try:
                    batch_pdf_path.unlink()
                except OSError:
                    LOGGER.warning(
                        "Could not remove temporary layout PDF %s.",
                        batch_pdf_path,
                    )

        write_json(
            output_root / "pymupdf4llm_layout_manifest.json",
            {
                "source_pdf": str(pdf_path),
                "layout_batch_size": args.layout_batch_size,
                "batches": layout_batch_manifest,
            },
        )
        if args.write_combined_layout_json:
            write_json(
                output_root / "pymupdf4llm_layout.json",
                {"pages": combined_layout_pages},
            )

        # Compare adjacent page pairs only after every selected table has a
        # neutral cell graph.  Auto-join high-confidence continuation tables,
        # preserve medium-confidence proposals for review, and then rebuild all
        # catalogue aggregates from the updated per-table checkpoints.
        continuation_joins: list[dict[str, Any]] = []
        continuation_reviews: list[dict[str, Any]] = []
        table_models: list[dict[str, Any]] = []
        table_model_paths: dict[tuple[int, int], Path] = {}
        for model_path in sorted(output_root.glob("page_*/table_*/table_model.json")):
            try:
                model = json.loads(model_path.read_text(encoding="utf-8"))
                table_models.append(model)
                table_model_paths[(int(model["page"]), int(model["table"]))] = model_path
            except Exception as exc:
                LOGGER.warning("Could not load table model %s: %s", model_path, exc)
        if not args.no_continuation_joins and table_models:
            continuation_joins, continuation_reviews = dtm.discover_and_apply_continuations(
                table_models,
                table_profile,
                auto_threshold=args.continuation_auto_threshold,
                review_threshold=args.continuation_review_threshold,
            )
            write_json(output_root / "continuation_joins.json", continuation_joins)
            write_json(output_root / "continuation_join_review.json", continuation_reviews)
            for model in table_models:
                key = (int(model["page"]), int(model["table"]))
                model_path = table_model_paths.get(key)
                if not model_path:
                    continue
                write_json(model_path, model)
                table_dir = model_path.parent
                pcols, prows = dtm.product_records_to_wide(model.get("product_records", []))
                write_csv(table_dir / "05_normalized_product_records.csv", [pcols, *prows])
                write_json(table_dir / "product_records.json", model.get("product_records", []))
                write_csv(table_dir / "product_records.csv", [pcols, *prows])
                related_joins = [
                    join for join in continuation_joins
                    if join.get("primary") == {"page": key[0], "table": key[1]}
                    or join.get("secondary") == {"page": key[0], "table": key[1]}
                ]
                if related_joins:
                    write_json(table_dir / "13_continuation_evidence.json", related_joins)
                final_path = table_dir / "final.json"
                if final_path.exists():
                    payload = json.loads(final_path.read_text(encoding="utf-8"))
                    payload["product_records"] = model.get("product_records", [])
                    payload["continuation_join_ids"] = [
                        join["join_id"] for join in continuation_joins
                        if join.get("primary") == {"page": key[0], "table": key[1]}
                        or join.get("secondary") == {"page": key[0], "table": key[1]}
                    ]
                    write_json(final_path, payload)

        # Rebuild aggregates from per-table final checkpoints.  This makes
        # continuation inheritance and resumed tables authoritative.
        all_product_records = []
        structured_tables = []
        long_rows = [["source_pdf", "page", "table", "method", "row_index", "column_index", "column_name", "value"]]
        product_long_rows = [["source_pdf", "page", "table", "product_code", "code_position", "attribute_name", "attribute_value", "source_rows", "source_columns", "method"]]
        for final_path in sorted(output_root.glob("page_*/table_*/final.json")):
            payload = json.loads(final_path.read_text(encoding="utf-8"))
            page_number = int(final_path.parent.parent.name.split("_")[-1])
            table_number = int(final_path.parent.name.split("_")[-1])
            columns = [clean_cell(value) for value in payload.get("columns", [])]
            rows = rectangularize(payload.get("rows", []))
            method = str(payload.get("method", "resumed"))
            records = payload.get("product_records", [])
            all_product_records.extend(records)
            product_long_rows.extend(product_records_long_rows(pdf_path.name, records))
            long_rows.extend(create_universal_long_rows(
                pdf_name=pdf_path.name, page_number=page_number, table_number=table_number,
                method=method, columns=columns, rows=rows,
            ))
            structured_tables.append({"page": page_number, "table": table_number, "columns": columns, "rows": rows, "method": method})

        write_csv(output_root / "all_tables_long.csv", long_rows)
        write_csv(output_root / "all_products_long.csv", product_long_rows)
        write_json(output_root / "all_product_records.json", all_product_records)
        all_product_columns, all_product_rows = dtm.product_records_to_wide(all_product_records)
        write_csv(
            output_root / "all_products_wide.csv",
            [all_product_columns, *all_product_rows],
        )

        product_code_registry = load_serialised_code_registry(
            registry_json_path
        )
        additional_review_rows = build_catalogue_product_outputs(
            output_root=output_root,
            pdf_name=pdf_path.name,
            catalogue_id=args.catalogue_id or pdf_path.stem,
            manufacturer=args.manufacturer or "",
            records=all_product_records,
            registry=product_code_registry,
            selected_pdf_pages=[page + 1 for page in selected_pages],
            page_count=doc.page_count,
        )
        review_queue.extend(additional_review_rows)
        for proposal in continuation_reviews:
            review_queue.append([
                pdf_path.name,
                proposal.get("primary", {}).get("page", ""),
                proposal.get("primary", {}).get("table", ""),
                "",
                "continuation_join_review",
                json.dumps(proposal, ensure_ascii=False),
                "",
                proposal.get("score", ""),
                "",
                "open",
                "",
                "",
            ])
        write_csv(output_root / "review_queue.csv", review_queue)

        extracted_locations: dict[str, list[str]] = collections.defaultdict(list)
        for record in all_product_records:
            key = normalize_code_key(str(record.get("product_code", "")))
            location = f"{record.get('source_page')}:{record.get('source_table')}"
            if key and location not in extracted_locations[key]:
                extracted_locations[key].append(location)
        canonical_keys = {
            key for key, entry in product_code_registry.items()
            if entry.get("canonical_registry")
        }
        manifest["product_records"] = len(all_product_records)
        manifest["extracted_unique_product_codes"] = len(extracted_locations)
        manifest["canonical_registry_codes"] = len(canonical_keys)
        manifest["unmatched_registry_codes"] = len(
            canonical_keys - set(extracted_locations)
        )
        manifest["unexpected_pdf_codes"] = len(
            set(extracted_locations) - canonical_keys
        )
        manifest["continuation_join_count"] = len(continuation_joins)
        manifest["continuation_join_review_count"] = len(continuation_reviews)
        write_json(output_root / "all_tables.json", structured_tables)
        write_json(output_root / "manifest.json", manifest)

        if structured_tables:
            schemas = {tuple(item["columns"]) for item in structured_tables}
            if len(schemas) == 1:
                columns = list(next(iter(schemas)))
                combined_rows: list[list[Any]] = [["source_page", "source_table", *columns]]
                for item in structured_tables:
                    for row in item["rows"]:
                        combined_rows.append([item["page"], item["table"], *row])
                write_csv(output_root / "all_tables_wide.csv", combined_rows)

    LOGGER.info("Finished. Outputs written to %s", output_root)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Extract PyMuPDF4LLM table boxes, combine compatible PyMuPDF grid "
            "geometry to expand merged cells, repair borderless columns, build "
            "a product-code registry, reconstruct hierarchical headers, classify "
            "dynamic table orientations, join continuation tables, emit faithful "
            "tables plus high-precision SKU records, and optionally route difficult "
            "tables to local Ollama."
        )
    )
    parser.add_argument(
        "pdf",
        nargs="?",
        type=Path,
        help="Input PDF catalogue. May also be supplied in --config.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        help=(
            "Optional JSON configuration file. Command-line values override "
            "configuration values."
        ),
    )
    parser.add_argument(
        "--catalogue-id",
        default="",
        help="Stable catalogue identifier used in consolidated outputs.",
    )
    parser.add_argument(
        "--manufacturer",
        default="",
        help="Manufacturer name written to consolidated outputs.",
    )
    parser.add_argument(
        "--pages",
        default="all",
        help=(
            "1-based pages such as '1-10,15,20-', 'all', or 'registry'. "
            "'registry' processes the mapped index pages plus the configured "
            "nearby-page radius (default: all)."
        ),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("catalogue_tables"),
        help="Output directory (default: catalogue_tables).",
    )
    parser.add_argument(
        "--layout-batch-size",
        type=int,
        default=10,
        help=(
            "Number of original PDF pages copied into each temporary "
            "PyMuPDF4LLM layout batch (default: 10). Smaller batches improve "
            "checkpointing and memory use on very large catalogues."
        ),
    )
    parser.add_argument(
        "--layout-process-mode",
        choices=("subprocess", "inline"),
        default="inline",
        help=(
            "Run PyMuPDF4LLM layout in an isolated subprocess or inline. "
            "Inline reuses one layout model and is the default. Subprocess "
            "isolates failures but requires more memory."
        ),
    )
    parser.add_argument(
        "--layout-timeout",
        type=int,
        default=1800,
        help="Maximum seconds allowed for one layout batch (default: 1800).",
    )
    parser.add_argument(
        "--write-combined-layout-json",
        action="store_true",
        help=(
            "Also write one combined pymupdf4llm_layout.json. Batch JSON files "
            "and a layout manifest are always written."
        ),
    )
    parser.add_argument(
        "--keep-layout-batch-pdfs",
        action="store_true",
        help=(
            "Keep the temporary subset PDFs used for PyMuPDF4LLM layout "
            "analysis. They are deleted by default after each batch succeeds."
        ),
    )
    parser.add_argument(
        "--use-ocr",
        action="store_true",
        help="Enable PyMuPDF4LLM OCR. Leave off for selectable-text catalogues.",
    )
    parser.add_argument(
        "--no-trim-sparse-edges",
        action="store_true",
        help="Disable deterministic removal of sparse left/right edge columns.",
    )
    parser.add_argument(
        "--no-hybrid-grid",
        action="store_true",
        help=(
            "Disable the PyMuPDF line-grid companion extractor. The hybrid grid "
            "is used only when its shape matches the PyMuPDF4LLM table and it "
            "provides reliable merged-cell placeholders."
        ),
    )
    parser.add_argument(
        "--no-word-column-repair",
        action="store_true",
        help=(
            "Disable native-word x-position repair for borderless tables where "
            "PyMuPDF4LLM merged logical columns."
        ),
    )
    parser.add_argument(
        "--word-column-min-support",
        type=float,
        default=0.50,
        help=(
            "Minimum fraction of data rows supporting a larger word-derived "
            "column count (default: 0.50)."
        ),
    )
    parser.add_argument(
        "--grid-min-score",
        type=float,
        default=0.82,
        help="Minimum compatibility score for using a line-grid table (default: 0.82).",
    )
    parser.add_argument(
        "--no-expand-merged-cells",
        action="store_true",
        help="Do not duplicate true rowspan/colspan values into covered rows/columns.",
    )
    parser.add_argument(
        "--product-code-regex",
        help=(
            "Optional regular expression used to identify product rows when "
            "building deterministic_structured.csv."
        ),
    )
    parser.add_argument(
        "--sku-registry",
        type=Path,
        help=(
            "Canonical unique-SKU registry CSV. Rows marked needs_review are "
            "included and flagged."
        ),
    )
    parser.add_argument(
        "--sku-index-rows",
        type=Path,
        help=(
            "Occurrence-level SKU index rows CSV containing printed catalogue "
            "page pointers and provenance."
        ),
    )
    parser.add_argument(
        "--catalogue-page-offset",
        default="auto",
        help=(
            "Mapping offset in 'PDF page = printed catalogue page + offset'. "
            "Use 'auto' or an integer (default: auto)."
        ),
    )
    parser.add_argument(
        "--index-page-radius",
        type=int,
        default=1,
        help=(
            "Include this many PDF pages before and after each mapped index "
            "target page when --pages registry is used (default: 1)."
        ),
    )
    parser.add_argument(
        "--review-queue-input",
        type=Path,
        help=(
            "Process open pages from a previous review_queue.csv. This takes "
            "priority over --pages and supports exception-only reruns."
        ),
    )
    parser.add_argument(
        "--product-code-index",
        type=Path,
        help=(
            "Optional previous product-code index/list in CSV, TSV, TXT or JSON. "
            "Every cell is inspected unless --product-code-column is supplied."
        ),
    )
    parser.add_argument(
        "--product-code-column",
        help="Column name containing codes in --product-code-index.",
    )
    parser.add_argument(
        "--code-registry-scope",
        choices=("all", "selected", "off"),
        default="all",
        help=(
            "Pages scanned with fast native text to build a code registry: the "
            "whole PDF, only selected pages, or none (default: all)."
        ),
    )
    parser.add_argument(
        "--registry-only",
        action="store_true",
        help=(
            "Build/import product_code_registry.csv and exit without running "
            "PyMuPDF4LLM table extraction."
        ),
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Reuse existing per-table final.json files and continue an interrupted run.",
    )
    parser.add_argument(
        "--min-column-fill",
        type=float,
        default=0.18,
        help="Maximum non-empty ratio for a sparse edge column (default: 0.18).",
    )
    parser.add_argument(
        "--max-edge-nonempty",
        type=int,
        default=2,
        help="Also regard an edge as sparse at or below this count (default: 2).",
    )
    parser.add_argument(
        "--render-dpi",
        type=int,
        default=140,
        help="DPI for AI/diagnostic images (default: 140).",
    )
    parser.add_argument(
        "--crop-padding",
        type=float,
        default=2.0,
        help="Padding around refined table crop in PDF points (default: 2).",
    )
    parser.add_argument(
        "--table-profile",
        type=Path,
        help=(
            "Optional reusable JSON profile containing SKU patterns, header "
            "aliases, non-SKU option codes, units, and footnote rules. The "
            "generic engine works without a profile."
        ),
    )
    parser.add_argument(
        "--no-continuation-joins",
        action="store_true",
        help="Disable automatic comparison and joining of adjacent-page table continuations.",
    )
    parser.add_argument(
        "--continuation-auto-threshold",
        type=float,
        default=0.90,
        help="Minimum confidence for an automatic adjacent-page continuation join (default: 0.90).",
    )
    parser.add_argument(
        "--continuation-review-threshold",
        type=float,
        default=0.70,
        help="Minimum confidence for writing a continuation join proposal to review (default: 0.70).",
    )
    parser.add_argument(
        "--overrides",
        type=Path,
        help=(
            "Optional JSON object keyed by 'page:table'; values may include "
            "drop_left_columns, drop_right_columns, or bbox."
        ),
    )
    parser.add_argument(
        "--schema",
        type=Path,
        help=(
            "Optional fixed target-column JSON schema used by the local AI "
            "stage. Omit it for table-specific dynamic columns."
        ),
    )
    parser.add_argument(
        "--ollama-model",
        help="Vision-capable local Ollama model. Omit to disable AI.",
    )
    parser.add_argument(
        "--ollama-url",
        default="http://localhost:11434",
        help="Ollama base URL or /api/chat endpoint (default: localhost:11434).",
    )
    parser.add_argument(
        "--ollama-timeout",
        type=int,
        default=1800,
        help="Seconds allowed for each Ollama table request (default: 1800).",
    )
    parser.add_argument(
        "--ollama-num-ctx",
        type=int,
        default=16384,
        help=(
            "Ollama context window requested for each API call. Vision images "
            "consume context tokens; 16384 is the recommended default for this "
            "two-pass workflow."
        ),
    )
    parser.add_argument(
        "--ollama-keep-alive",
        default="30m",
        help=(
            "How long Ollama should retain the model after a request "
            "(default: 30m). This avoids reloading it between the two AI passes."
        ),
    )
    parser.add_argument(
        "--ai-mode",
        choices=("validate", "structure"),
        default="validate",
        help=(
            "validate runs first-pass AI evaluation/refinement but keeps the "
            "deterministic final CSV; structure additionally runs the second-pass "
            "CSV structuring stage, subject to confidence gating (default: validate)."
        ),
    )
    parser.add_argument(
        "--ai-review-policy",
        choices=("auto", "always", "never"),
        default="auto",
        help=(
            "When to run the expensive vision boundary-review call. auto routes "
            "only suspicious tables; always reviews every table; never skips it "
            "(default: auto)."
        ),
    )
    parser.add_argument(
        "--ai-structure-input",
        choices=("auto", "text", "crop"),
        default="auto",
        help=(
            "Input for CSV structuring. auto normally uses the normalized text "
            "matrix and adds one crop only for visual-only columns; text is "
            "fastest; crop always sends an image (default: auto)."
        ),
    )
    parser.add_argument(
        "--ai-max-image-width",
        type=int,
        default=1200,
        help="Maximum width of compact images sent to Ollama (default: 1200).",
    )
    parser.add_argument(
        "--min-ai-review-confidence",
        type=float,
        default=0.65,
        help=(
            "Minimum first-pass confidence before an AI boundary correction is "
            "applied (default: 0.65)."
        ),
    )
    parser.add_argument(
        "--min-ai-structure-confidence",
        type=float,
        default=0.70,
        help=(
            "Minimum second-pass confidence before AI rows become final.csv "
            "(default: 0.70)."
        ),
    )
    parser.add_argument(
        "--ai-low-confidence-action",
        choices=("keep-deterministic", "use-ai", "fail"),
        default="keep-deterministic",
        help=(
            "What structure mode does when AI confidence is below a threshold: "
            "keep deterministic final.csv, use AI anyway, or fail "
            "(default: keep-deterministic)."
        ),
    )
    parser.add_argument(
        "--fail-on-ai-error",
        action="store_true",
        help="Abort instead of falling back to deterministic output when AI fails.",
    )
    parser.add_argument(
        "--log-level",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        default="INFO",
    )
    return parser


CONFIG_PATH_FIELDS = {
    "pdf",
    "output",
    "sku_registry",
    "sku_index_rows",
    "product_code_index",
    "overrides",
    "schema",
    "review_queue_input",
    "table_profile",
}


def parse_args_with_config() -> tuple[argparse.ArgumentParser, argparse.Namespace]:
    pre_parser = argparse.ArgumentParser(add_help=False)
    pre_parser.add_argument("--config", type=Path)
    known, _ = pre_parser.parse_known_args()

    parser = build_parser()
    if known.config:
        if not known.config.exists():
            parser.error(f"Configuration file not found: {known.config}")
        payload = json.loads(known.config.read_text(encoding="utf-8-sig"))
        if not isinstance(payload, dict):
            parser.error("Configuration JSON must contain an object.")
        valid_dests = {
            action.dest
            for action in parser._actions
            if action.dest not in {"help"}
        }
        defaults: dict[str, Any] = {}
        for raw_key, raw_value in payload.items():
            key = str(raw_key).replace("-", "_")
            if key not in valid_dests:
                parser.error(
                    f"Unknown configuration key {raw_key!r}. "
                    "Run --help to see available options."
                )
            if key in CONFIG_PATH_FIELDS and raw_value not in {None, ""}:
                defaults[key] = Path(str(raw_value))
            else:
                defaults[key] = raw_value
        defaults["config"] = known.config
        parser.set_defaults(**defaults)

    args = parser.parse_args()
    if args.pdf is None:
        parser.error("An input PDF is required, either as an argument or in --config.")
    return parser, args


def main() -> int:
    parser, args = parse_args_with_config()
    if not 0.2 <= args.word_column_min_support <= 1.0:
        parser.error("--word-column-min-support must be between 0.2 and 1.0.")
    if args.index_page_radius < 0:
        parser.error("--index-page-radius must be zero or greater.")
    if args.layout_batch_size < 1:
        parser.error("--layout-batch-size must be at least 1.")
    if args.layout_timeout < 1:
        parser.error("--layout-timeout must be at least 1 second.")
    if args.ollama_num_ctx < 8192:
        parser.error(
            "--ollama-num-ctx must be at least 8192. Use 16384 when vision "
            "review or large structured outputs are expected."
        )
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(levelname)s: %(message)s",
    )
    try:
        return extract_catalogue(args)
    except (ValueError, FileNotFoundError, json.JSONDecodeError) as exc:
        LOGGER.error("%s", exc)
        return 2
    except KeyboardInterrupt:
        LOGGER.error("Interrupted.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
