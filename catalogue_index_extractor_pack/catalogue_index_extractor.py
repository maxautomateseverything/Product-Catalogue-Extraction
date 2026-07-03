#!/usr/bin/env python3
"""
Configurable PDF catalogue index extractor.

Purpose
-------
Extract a one-row-per-SKU registry from selectable-text PDF catalogue index pages.
The minimum extracted fields are:
    - Product Code / SKU
    - Catalogue Page Number

The primary extraction engine is pdfplumber word-coordinate extraction.
The script detects configured column headers, builds vertical column zones, pairs
product codes with page references by top-to-bottom order, and audits all raw
selectable-text SKU candidates so likely product codes are not silently lost.

Run modes
---------
Interactive first run:
    py catalogue_index_extractor.py --interactive
te
Run from saved config:
    py catalogue_index_extractor.py --config catalogue_index_config.yaml

Dependencies
------------
    pip install pdfplumber PyYAML pillow

Notes
-----
This tool is intentionally semi-guided. It is designed for selectable-text PDFs,
not scanned/OCR-only catalogues.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import textwrap
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

try:
    import pdfplumber
except ImportError as exc:  # pragma: no cover
    raise SystemExit(
        "Missing dependency: pdfplumber. Install with: py -m pip install pdfplumber"
    ) from exc

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None


# -----------------------------
# Console helpers
# -----------------------------


def log(message: str) -> None:
    ts = time.strftime("%H:%M:%S")
    print(f"[{ts}] {message}", flush=True)


def prompt_text(prompt: str, default: Optional[str] = None, required: bool = True) -> str:
    while True:
        suffix = f" [{default}]" if default not in (None, "") else ""
        value = input(f"{prompt}{suffix}: ").strip()
        if not value and default is not None:
            return str(default)
        if value or not required:
            return value
        print("This value is required.")


def prompt_yes_no(prompt: str, default: bool = False) -> bool:
    default_label = "Y/n" if default else "y/N"
    while True:
        value = input(f"{prompt} ({default_label}): ").strip().lower()
        if not value:
            return default
        if value in {"y", "yes"}:
            return True
        if value in {"n", "no"}:
            return False
        print("Please enter y or n.")


def prompt_int(prompt: str, default: Optional[int] = None, minimum: Optional[int] = None) -> int:
    while True:
        suffix = f" [{default}]" if default is not None else ""
        value = input(f"{prompt}{suffix}: ").strip()
        if not value and default is not None:
            return default
        try:
            result = int(value)
            if minimum is not None and result < minimum:
                print(f"Please enter a number >= {minimum}.")
                continue
            return result
        except ValueError:
            print("Please enter a whole number.")


def prompt_list(prompt: str, minimum: int = 0, help_text: Optional[str] = None) -> List[str]:
    if help_text:
        print(help_text)
    values: List[str] = []
    i = 1
    while True:
        value = input(f"{prompt} {i} ({'blank to finish' if len(values) >= minimum else 'required'}): ").strip()
        if not value:
            if len(values) >= minimum:
                return values
            print(f"Please provide at least {minimum} value(s).")
            continue
        values.append(value)
        i += 1


# -----------------------------
# Config loading/saving
# -----------------------------


def load_config(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        if path.suffix.lower() in {".yaml", ".yml"}:
            if yaml is None:
                raise SystemExit("PyYAML is required to read YAML config files. Install with: py -m pip install PyYAML")
            return yaml.safe_load(f) or {}
        return json.load(f)


def save_config(config: Dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        if path.suffix.lower() in {".yaml", ".yml"}:
            if yaml is None:
                json.dump(config, f, indent=2)
            else:
                yaml.safe_dump(config, f, sort_keys=False, allow_unicode=True)
        else:
            json.dump(config, f, indent=2, ensure_ascii=False)


# -----------------------------
# Generic utilities
# -----------------------------


def parse_page_range(page_range: str) -> List[int]:
    """Parse ranges such as '1-3,7,10-12' into 1-based PDF page numbers."""
    pages: List[int] = []
    for part in str(page_range).split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            start_s, end_s = part.split("-", 1)
            start = int(start_s.strip())
            end = int(end_s.strip())
            step = 1 if end >= start else -1
            pages.extend(list(range(start, end + step, step)))
        else:
            pages.append(int(part))
    seen = set()
    ordered = []
    for p in pages:
        if p not in seen:
            seen.add(p)
            ordered.append(p)
    return ordered


def collapse_ws(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def normalize_for_match(text: str, case_sensitive: bool) -> str:
    text = collapse_ws(text)
    return text if case_sensitive else text.lower()


def normalize_sku(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", str(text or "")).upper()


def normalize_page_value(text: str) -> str:
    """Keep a normalized page-list value while preserving alphanumeric page refs.

    Examples:
      '12, 13' -> '12;13'
      '12/13' -> '12;13'
      'See page 12' -> '12'
      'A12' -> 'A12'
    """
    raw = collapse_ws(text)
    if not raw:
        return ""

    # Pull page-like tokens after common phrase wrappers, while preserving A12-like refs.
    cleaned = re.sub(r"(?i)\bsee\s+page\b", "", raw).strip()
    cleaned = re.sub(r"(?i)\bpage\b", "", cleaned).strip()

    # Numeric/alphanumeric refs separated by comma, slash, semicolon, or simple whitespace.
    # Do not expand ranges because catalogues may use '12-13' as an exact printed reference.
    if re.fullmatch(r"[A-Za-z]?\d+(?:\s*[,/;]\s*[A-Za-z]?\d+)+", cleaned):
        parts = re.split(r"\s*[,/;]\s*", cleaned)
        return ";".join(p for p in parts if p)

    return cleaned


def dedupe_preserve(values: Iterable[str]) -> List[str]:
    seen = set()
    out: List[str] = []
    for value in values:
        value = collapse_ws(value)
        if value and value not in seen:
            seen.add(value)
            out.append(value)
    return out


def safe_review_value(value: Any) -> Any:
    """Excel-safe value for review files only.

    This intentionally changes only review/audit files, not the main collected data file.
    """
    if value is None:
        return ""
    s = str(value)
    if not s:
        return ""
    # Avoid double wrapping existing formulas.
    s = s.replace('"', '""')
    return f'="{s}"'


def compile_regex(pattern: str, label: str) -> re.Pattern[str]:
    try:
        return re.compile(pattern)
    except re.error as exc:
        raise SystemExit(f"Invalid {label} regex: {exc}\nPattern: {pattern}") from exc


# -----------------------------
# Regex suggestion helpers
# -----------------------------


def suggest_sku_regex(examples: Sequence[str]) -> str:
    """Suggest a broad-but-editable SKU regex from positive examples.

    This is deliberately conservative about section headers by requiring at least
    two code groups after a detected prefix when examples appear space-separated.
    The user should approve or edit this regex before running.
    """
    cleaned = [collapse_ws(e).upper() for e in examples if collapse_ws(e)]
    first_tokens = []
    has_space = False
    for ex in cleaned:
        if " " in ex:
            has_space = True
        token = re.split(r"[\s\-_/\.]+", ex, maxsplit=1)[0]
        if token:
            first_tokens.append(re.escape(token))
    prefixes = dedupe_preserve(first_tokens)

    if prefixes and has_space:
        prefix_alt = "|".join(prefixes)
        return rf"\b(?:{prefix_alt})(?:[\s\-_/\.]+[A-Z0-9]{{1,8}}){{2,5}}\b"

    # Fallback for compact or mixed catalogue codes. This is intentionally broad.
    return r"\b[A-Z0-9][A-Z0-9\-_/\.]{2,40}\b"


def suggest_page_regex(examples: Sequence[str]) -> str:
    # Broad enough for numeric, A12, 12/13, 12, 13, 12-13, and 'See page 12'.
    return r"(?i)\b(?:see\s+page\s+)?[A-Z]?\d+(?:\s*(?:,|/|;|-)\s*[A-Z]?\d+)*\b"


# -----------------------------
# PDF word/line handling
# -----------------------------


@dataclass
class WordLine:
    words: List[Dict[str, Any]]
    top: float
    bottom: float
    x0: float
    x1: float
    text: str


@dataclass
class HeaderOccurrence:
    text: str
    field_name: str
    x0: float
    x1: float
    top: float
    bottom: float
    center_x: float
    center_y: float
    pdf_page: int


@dataclass
class ColumnZone:
    field_name: str
    header_text: str
    x0: float
    x1: float
    header_found: bool = True


@dataclass
class TableBlock:
    block_number: int
    pdf_page: int
    block_x0: float
    block_x1: float
    header_top: float
    header_bottom: float
    sku_header: Optional[HeaderOccurrence]
    page_header: Optional[HeaderOccurrence]
    column_zones: Dict[str, ColumnZone]
    detection_mode: str = "auto"
    issues: List[str] = field(default_factory=list)


@dataclass
class ExtractedValue:
    value: str
    normalized: str
    top: float
    bottom: float
    x0: float
    x1: float
    source_line_text: str
    status: str = "ok"
    review_reason: str = ""


def word_center(word: Dict[str, Any]) -> Tuple[float, float]:
    return ((float(word["x0"]) + float(word["x1"])) / 2, (float(word["top"]) + float(word["bottom"])) / 2)


def group_words_to_lines(words: Sequence[Dict[str, Any]], y_tolerance: float = 3.0) -> List[WordLine]:
    """Group pdfplumber words into visual lines by y-center."""
    if not words:
        return []

    sorted_words = sorted(words, key=lambda w: (word_center(w)[1], float(w["x0"])))
    groups: List[List[Dict[str, Any]]] = []
    group_centers: List[float] = []

    for word in sorted_words:
        _, cy = word_center(word)
        placed = False
        for idx, center in enumerate(group_centers):
            if abs(cy - center) <= y_tolerance:
                groups[idx].append(word)
                # moving average keeps the group stable across tiny PDF coordinate differences
                group_centers[idx] = (group_centers[idx] * (len(groups[idx]) - 1) + cy) / len(groups[idx])
                placed = True
                break
        if not placed:
            groups.append([word])
            group_centers.append(cy)

    lines: List[WordLine] = []
    for group in groups:
        group_sorted = sorted(group, key=lambda w: float(w["x0"]))
        text = collapse_ws(" ".join(str(w.get("text", "")) for w in group_sorted))
        if not text:
            continue
        lines.append(
            WordLine(
                words=group_sorted,
                top=min(float(w["top"]) for w in group_sorted),
                bottom=max(float(w["bottom"]) for w in group_sorted),
                x0=min(float(w["x0"]) for w in group_sorted),
                x1=max(float(w["x1"]) for w in group_sorted),
                text=text,
            )
        )
    return sorted(lines, key=lambda ln: (ln.top, ln.x0))


def find_exact_header_occurrences(
    words: Sequence[Dict[str, Any]],
    header_text: str,
    field_name: str,
    pdf_page: int,
    case_sensitive: bool,
    y_tolerance: float = 3.0,
) -> List[HeaderOccurrence]:
    """Find exact header text occurrences by matching token sequences in visual lines."""
    target_tokens = [normalize_for_match(t, case_sensitive) for t in collapse_ws(header_text).split()]
    if not target_tokens:
        return []

    occurrences: List[HeaderOccurrence] = []
    for line in group_words_to_lines(words, y_tolerance=y_tolerance):
        line_words = sorted(line.words, key=lambda w: float(w["x0"]))
        tokens = [normalize_for_match(str(w.get("text", "")), case_sensitive) for w in line_words]
        for start in range(0, len(tokens) - len(target_tokens) + 1):
            if tokens[start : start + len(target_tokens)] == target_tokens:
                matched_words = line_words[start : start + len(target_tokens)]
                x0 = min(float(w["x0"]) for w in matched_words)
                x1 = max(float(w["x1"]) for w in matched_words)
                top = min(float(w["top"]) for w in matched_words)
                bottom = max(float(w["bottom"]) for w in matched_words)
                occurrences.append(
                    HeaderOccurrence(
                        text=header_text,
                        field_name=field_name,
                        x0=x0,
                        x1=x1,
                        top=top,
                        bottom=bottom,
                        center_x=(x0 + x1) / 2,
                        center_y=(top + bottom) / 2,
                        pdf_page=pdf_page,
                    )
                )
    return sorted(occurrences, key=lambda h: (h.center_y, h.center_x))


def is_same_header_line(a: HeaderOccurrence, b: HeaderOccurrence, tolerance: float = 6.0) -> bool:
    return abs(a.center_y - b.center_y) <= tolerance


# -----------------------------
# Header/table block detection
# -----------------------------


def detect_auto_blocks(
    page_width: float,
    pdf_page: int,
    words: Sequence[Dict[str, Any]],
    config: Dict[str, Any],
) -> Tuple[List[TableBlock], List[Dict[str, Any]]]:
    required = config["required_columns"]
    optional_columns = config.get("optional_columns", []) or []
    case_sensitive = bool(config.get("header_matching", {}).get("case_sensitive", False))
    header_y_tol = float(config.get("advanced", {}).get("header_y_tolerance", 6.0))

    sku_header_text = required["sku"]["header_text"]
    page_header_text = required["page"]["header_text"]

    sku_headers = find_exact_header_occurrences(words, sku_header_text, "sku", pdf_page, case_sensitive)
    page_headers = find_exact_header_occurrences(words, page_header_text, "page", pdf_page, case_sensitive)

    optional_headers_by_name: Dict[str, List[HeaderOccurrence]] = {}
    for col in optional_columns:
        output_name = col["output_name"]
        optional_headers_by_name[output_name] = find_exact_header_occurrences(
            words, col["header_text"], output_name, pdf_page, case_sensitive
        )

    detection_records: List[Dict[str, Any]] = []
    for h in sku_headers + page_headers + [h for hs in optional_headers_by_name.values() for h in hs]:
        detection_records.append(
            {
                "source_pdf_page": pdf_page,
                "field_name": h.field_name,
                "header_text": h.text,
                "x0": round(h.x0, 3),
                "x1": round(h.x1, 3),
                "top": round(h.top, 3),
                "bottom": round(h.bottom, 3),
                "center_x": round(h.center_x, 3),
                "center_y": round(h.center_y, 3),
                "detection_mode": "auto_header_search",
            }
        )

    # Pair each SKU header with nearest Page header to the right on the same visual header row.
    used_page_header_ids = set()
    pairs: List[Tuple[HeaderOccurrence, HeaderOccurrence]] = []
    for sku_h in sorted(sku_headers, key=lambda h: h.center_x):
        candidates = [
            p
            for p in page_headers
            if id(p) not in used_page_header_ids
            and p.center_x > sku_h.center_x
            and is_same_header_line(sku_h, p, header_y_tol)
        ]
        if not candidates:
            continue
        page_h = sorted(candidates, key=lambda p: p.center_x - sku_h.center_x)[0]
        used_page_header_ids.add(id(page_h))
        pairs.append((sku_h, page_h))

    pairs = sorted(pairs, key=lambda pair: pair[0].center_x)
    blocks: List[TableBlock] = []
    for i, (sku_h, page_h) in enumerate(pairs, start=1):
        prev_pair = pairs[i - 2] if i > 1 else None
        next_pair = pairs[i] if i < len(pairs) else None

        block_x0 = 0.0 if prev_pair is None else (prev_pair[1].center_x + sku_h.center_x) / 2
        block_x1 = page_width if next_pair is None else (page_h.center_x + next_pair[0].center_x) / 2

        header_occurrences: Dict[str, HeaderOccurrence] = {"sku": sku_h, "page": page_h}
        for col in optional_columns:
            name = col["output_name"]
            candidates = [
                h
                for h in optional_headers_by_name.get(name, [])
                if block_x0 <= h.center_x <= block_x1 and is_same_header_line(sku_h, h, header_y_tol)
            ]
            if candidates:
                header_occurrences[name] = sorted(candidates, key=lambda h: abs(h.center_x - sku_h.center_x))[0]

        sorted_headers = sorted(header_occurrences.items(), key=lambda item: item[1].center_x)
        centers = [h.center_x for _, h in sorted_headers]
        zones: Dict[str, ColumnZone] = {}
        for idx, (field_name, h) in enumerate(sorted_headers):
            left = block_x0 if idx == 0 else (centers[idx - 1] + centers[idx]) / 2
            right = block_x1 if idx == len(sorted_headers) - 1 else (centers[idx] + centers[idx + 1]) / 2
            zones[field_name] = ColumnZone(field_name=field_name, header_text=h.text, x0=left, x1=right)

        issues: List[str] = []
        for col in optional_columns:
            name = col["output_name"]
            if name not in zones:
                issues.append(f"optional_header_not_found:{name}")

        block = TableBlock(
            block_number=i,
            pdf_page=pdf_page,
            block_x0=block_x0,
            block_x1=block_x1,
            header_top=min(sku_h.top, page_h.top),
            header_bottom=max(sku_h.bottom, page_h.bottom),
            sku_header=sku_h,
            page_header=page_h,
            column_zones=zones,
            detection_mode="auto",
            issues=issues,
        )
        blocks.append(block)

    return blocks, detection_records


def manual_blocks_from_config(
    page_width: float,
    pdf_page: int,
    config: Dict[str, Any],
) -> List[TableBlock]:
    blocks_cfg = config.get("manual_coordinate_blocks", []) or []
    blocks: List[TableBlock] = []
    for idx, block_cfg in enumerate(blocks_cfg, start=1):
        zones: Dict[str, ColumnZone] = {}
        block_x0 = float(block_cfg.get("block_x0", 0.0))
        block_x1 = float(block_cfg.get("block_x1", page_width))
        for field_name, zone_cfg in block_cfg.get("columns", {}).items():
            zones[field_name] = ColumnZone(
                field_name=field_name,
                header_text=zone_cfg.get("header_text", field_name),
                x0=float(zone_cfg["x0"]),
                x1=float(zone_cfg["x1"]),
                header_found=False,
            )
        blocks.append(
            TableBlock(
                block_number=idx,
                pdf_page=pdf_page,
                block_x0=block_x0,
                block_x1=block_x1,
                header_top=float(block_cfg.get("data_top", 0.0)),
                header_bottom=float(block_cfg.get("data_top", 0.0)),
                sku_header=None,
                page_header=None,
                column_zones=zones,
                detection_mode="manual",
                issues=[],
            )
        )
    return blocks


# -----------------------------
# Column value extraction
# -----------------------------


def words_in_zone(
    words: Sequence[Dict[str, Any]],
    x0: float,
    x1: float,
    y_min: float,
    y_max: float,
) -> List[Dict[str, Any]]:
    out = []
    for word in words:
        cx, cy = word_center(word)
        if x0 <= cx <= x1 and y_min <= cy <= y_max:
            out.append(word)
    return out


def extract_sku_values(
    words: Sequence[Dict[str, Any]],
    zone: ColumnZone,
    y_min: float,
    y_max: float,
    sku_regex: re.Pattern[str],
    y_tolerance: float,
) -> List[ExtractedValue]:
    zone_words = words_in_zone(words, zone.x0, zone.x1, y_min, y_max)
    values: List[ExtractedValue] = []
    for line in group_words_to_lines(zone_words, y_tolerance=y_tolerance):
        for match in sku_regex.finditer(line.text):
            sku = collapse_ws(match.group(0))
            if not sku:
                continue
            values.append(
                ExtractedValue(
                    value=sku,
                    normalized=normalize_sku(sku),
                    top=line.top,
                    bottom=line.bottom,
                    x0=line.x0,
                    x1=line.x1,
                    source_line_text=line.text,
                )
            )
    return sorted(values, key=lambda v: (v.top, v.x0))


def extract_text_values(
    words: Sequence[Dict[str, Any]],
    zone: ColumnZone,
    y_min: float,
    y_max: float,
    y_tolerance: float,
    value_regex: Optional[re.Pattern[str]] = None,
    keep_non_matching: bool = True,
    normalizer: Optional[Any] = None,
) -> List[ExtractedValue]:
    zone_words = words_in_zone(words, zone.x0, zone.x1, y_min, y_max)
    values: List[ExtractedValue] = []
    for line in group_words_to_lines(zone_words, y_tolerance=y_tolerance):
        text = collapse_ws(line.text)
        if not text:
            continue
        extracted = text
        status = "ok"
        reason = ""
        if value_regex is not None:
            m = value_regex.search(text)
            if m:
                extracted = collapse_ws(m.group(0))
            elif keep_non_matching:
                status = "needs_review"
                reason = "value_did_not_match_regex"
            else:
                continue
        normalized = normalizer(extracted) if normalizer else extracted
        values.append(
            ExtractedValue(
                value=extracted,
                normalized=normalized,
                top=line.top,
                bottom=line.bottom,
                x0=line.x0,
                x1=line.x1,
                source_line_text=line.text,
                status=status,
                review_reason=reason,
            )
        )
    return sorted(values, key=lambda v: (v.top, v.x0))


def extract_rows_from_block(
    pdf_page: int,
    page_height: float,
    words: Sequence[Dict[str, Any]],
    block: TableBlock,
    config: Dict[str, Any],
    sku_regex: re.Pattern[str],
    page_regex: Optional[re.Pattern[str]],
) -> Tuple[List[Dict[str, Any]], List[str]]:
    advanced = config.get("advanced", {})
    y_tolerance = float(advanced.get("line_y_tolerance", 3.0))
    y_min = block.header_bottom + float(advanced.get("data_start_padding", 1.0))
    y_max = float(advanced.get("data_bottom", page_height))

    zones = block.column_zones
    issues: List[str] = list(block.issues)
    rows: List[Dict[str, Any]] = []

    if "sku" not in zones or "page" not in zones:
        issues.append("required_column_zone_missing")
        return rows, issues

    sku_values = extract_sku_values(words, zones["sku"], y_min, y_max, sku_regex, y_tolerance)
    page_values = extract_text_values(
        words,
        zones["page"],
        y_min,
        y_max,
        y_tolerance,
        value_regex=page_regex,
        keep_non_matching=True,
        normalizer=normalize_page_value,
    )

    if len(sku_values) != len(page_values):
        issues.append(f"sku_page_count_mismatch:sku={len(sku_values)};page={len(page_values)}")

    optional_values_by_name: Dict[str, List[ExtractedValue]] = {}
    optional_columns = config.get("optional_columns", []) or []
    for col in optional_columns:
        name = col["output_name"]
        if name not in zones:
            optional_values_by_name[name] = []
            continue
        optional_values_by_name[name] = extract_text_values(
            words,
            zones[name],
            y_min,
            y_max,
            y_tolerance,
            value_regex=None,
            keep_non_matching=True,
            normalizer=lambda s: collapse_ws(s),
        )
        if optional_values_by_name[name] and len(optional_values_by_name[name]) != len(sku_values):
            issues.append(f"optional_count_mismatch:{name}={len(optional_values_by_name[name])};sku={len(sku_values)}")

    max_rows = max(len(sku_values), len(page_values), 0)
    for i in range(max_rows):
        sku_val = sku_values[i] if i < len(sku_values) else None
        page_val = page_values[i] if i < len(page_values) else None

        row_issues: List[str] = []
        if sku_val is None:
            row_issues.append("missing_sku_for_page_value")
        if page_val is None:
            row_issues.append("missing_page_for_sku")
        if sku_val and page_val and page_val.status != "ok":
            row_issues.append(page_val.review_reason or page_val.status)
        if len(sku_values) != len(page_values):
            row_issues.append("sku_page_count_mismatch_in_block")

        row: Dict[str, Any] = {
            "sku": sku_val.value if sku_val else "",
            "sku_normalized": sku_val.normalized if sku_val else "",
            "catalogue_page_original": page_val.value if page_val else "",
            "catalogue_page_normalized": page_val.normalized if page_val else "",
            "source_pdf_page": pdf_page,
            "source_table_block": block.block_number,
            "source_method": "pdfplumber_coordinate",
            "detection_mode": block.detection_mode,
            "sku_top": round(sku_val.top, 3) if sku_val else "",
            "page_top": round(page_val.top, 3) if page_val else "",
            "confidence_status": "needs_review" if row_issues else "confirmed",
            "review_reason": ";".join(dedupe_preserve(row_issues)),
            "source_sku_line_text": sku_val.source_line_text if sku_val else "",
            "source_page_line_text": page_val.source_line_text if page_val else "",
        }

        for col in optional_columns:
            name = col["output_name"]
            vals = optional_values_by_name.get(name, [])
            opt_val = vals[i] if i < len(vals) else None
            row[name] = opt_val.value if opt_val else ""
            row[f"{name}_confidence"] = "confirmed" if opt_val and not row_issues else ("missing" if not opt_val else "needs_review")
            if vals and len(vals) != len(sku_values):
                row["confidence_status"] = "needs_review"
                reasons = dedupe_preserve((row.get("review_reason", "") + f";optional_count_mismatch:{name}").split(";"))
                row["review_reason"] = ";".join(reasons)

        # Keep only rows with at least one SKU or page value. Registry later keeps SKU rows.
        if row["sku"] or row["catalogue_page_original"]:
            rows.append(row)

    return rows, issues


# -----------------------------
# Raw text audit
# -----------------------------


def raw_text_sku_audit_for_page(
    page: Any,
    pdf_page: int,
    sku_regex: re.Pattern[str],
) -> List[Dict[str, Any]]:
    text = page.extract_text(layout=True) or page.extract_text() or ""
    rows: List[Dict[str, Any]] = []
    for match_idx, match in enumerate(sku_regex.finditer(text), start=1):
        sku = collapse_ws(match.group(0))
        if not sku:
            continue
        start = max(0, match.start() - 60)
        end = min(len(text), match.end() + 60)
        context = collapse_ws(text[start:end].replace("\n", " "))
        rows.append(
            {
                "source_pdf_page": pdf_page,
                "raw_occurrence_number": match_idx,
                "sku": sku,
                "sku_normalized": normalize_sku(sku),
                "context": context,
                "in_structured_output_before_raw_add": False,
                "added_to_registry_from_raw_text": False,
            }
        )
    return rows


# -----------------------------
# Interactive manual coordinate setup
# -----------------------------


def print_first_candidate_coordinates(
    pdf_path: Path,
    first_pdf_page: int,
    sku_regex: re.Pattern[str],
    page_regex: Optional[re.Pattern[str]],
) -> None:
    log(f"Inspecting first configured page for candidate coordinates: PDF page {first_pdf_page}")
    try:
        with pdfplumber.open(str(pdf_path)) as pdf:
            page = pdf.pages[first_pdf_page - 1]
            words = page.extract_words(x_tolerance=1, y_tolerance=3, keep_blank_chars=False, use_text_flow=False)
            lines = group_words_to_lines(words, y_tolerance=3.0)

            sku_candidates = []
            page_candidates = []
            for line in lines:
                for m in sku_regex.finditer(line.text):
                    sku_candidates.append((collapse_ws(m.group(0)), line))
                if page_regex:
                    for m in page_regex.finditer(line.text):
                        page_candidates.append((collapse_ws(m.group(0)), line))

            print("\nFirst SKU candidate line coordinates:")
            for idx, (sku, line) in enumerate(sku_candidates[:20], start=1):
                print(
                    f"  {idx:>2}. {sku:<30} x0={line.x0:>7.2f} x1={line.x1:>7.2f} "
                    f"top={line.top:>7.2f} bottom={line.bottom:>7.2f} | {line.text[:120]}"
                )

            if page_regex:
                print("\nFirst page-value candidate line coordinates:")
                for idx, (pg, line) in enumerate(page_candidates[:20], start=1):
                    print(
                        f"  {idx:>2}. {pg:<20} x0={line.x0:>7.2f} x1={line.x1:>7.2f} "
                        f"top={line.top:>7.2f} bottom={line.bottom:>7.2f} | {line.text[:120]}"
                    )
            print("")
    except Exception as exc:
        print(f"Could not print candidate coordinates: {exc}")


def prompt_float_range(label: str) -> Tuple[float, float]:
    while True:
        value = input(f"{label} x-range as x0,x1: ").strip()
        try:
            x0_s, x1_s = value.split(",", 1)
            x0 = float(x0_s.strip())
            x1 = float(x1_s.strip())
            if x1 <= x0:
                print("x1 must be greater than x0.")
                continue
            return x0, x1
        except Exception:
            print("Please enter two numbers separated by a comma, for example: 35,90")


def prompt_manual_blocks(config: Dict[str, Any]) -> None:
    pdf_path = Path(config["input_pdf"])
    pages = parse_page_range(config["index_pdf_pages"])
    sku_regex = compile_regex(config["sku_detection"]["sku_regex"], "SKU")
    page_regex = compile_regex(config["page_detection"]["page_regex"], "page") if config.get("page_detection", {}).get("page_regex") else None
    print_first_candidate_coordinates(pdf_path, pages[0], sku_regex, page_regex)

    expected = int(config.get("expected_table_blocks_per_page", 1))
    block_count = prompt_int("How many manual table blocks do you want to define?", expected, minimum=1)
    blocks: List[Dict[str, Any]] = []

    optional_columns = config.get("optional_columns", []) or []
    for block_idx in range(1, block_count + 1):
        print(f"\nManual coordinates for table block {block_idx}")
        sku_x0, sku_x1 = prompt_float_range("SKU column")
        page_x0, page_x1 = prompt_float_range("Catalogue page column")
        data_top_raw = prompt_text("Data top y-coordinate (blank = 0)", default="", required=False)
        data_top = float(data_top_raw) if data_top_raw else 0.0
        columns = {
            "sku": {"x0": sku_x0, "x1": sku_x1, "header_text": config["required_columns"]["sku"]["header_text"]},
            "page": {"x0": page_x0, "x1": page_x1, "header_text": config["required_columns"]["page"]["header_text"]},
        }
        all_xs = [sku_x0, sku_x1, page_x0, page_x1]
        for col in optional_columns:
            name = col["output_name"]
            if prompt_yes_no(f"Define manual x-range for optional column '{name}'?", default=False):
                x0, x1 = prompt_float_range(f"Optional column '{name}'")
                columns[name] = {"x0": x0, "x1": x1, "header_text": col["header_text"]}
                all_xs.extend([x0, x1])
        blocks.append({"block_x0": min(all_xs), "block_x1": max(all_xs), "data_top": data_top, "columns": columns})

    config["manual_coordinate_blocks"] = blocks


# -----------------------------
# Interactive config builder
# -----------------------------


def build_interactive_config() -> Dict[str, Any]:
    print("\nCatalogue Index Extractor - Interactive Setup")
    print("This creates a reusable config and then runs the extraction.\n")

    input_pdf = prompt_text("Input PDF path")
    output_folder = prompt_text("Output folder", default=str(Path(input_pdf).with_name("index_output")))
    index_pdf_pages = prompt_text("Index PDF page range (example: 1293-1364)")

    case_sensitive = prompt_yes_no("Should header matching be case-sensitive?", default=False)
    sku_header = prompt_text("Exact product-code/SKU column header text", default="Code")
    page_header = prompt_text("Exact catalogue-page column header text", default="Page")
    expected_blocks = prompt_int("Expected product-code/page table blocks per page", default=1, minimum=1)

    optional_columns: List[Dict[str, str]] = []
    if prompt_yes_no("Do you want to configure optional columns?", default=True):
        print("Enter optional column headers. The output name can be blank to auto-generate one.")
        while True:
            header = prompt_text("Optional column exact header text", required=False)
            if not header:
                break
            suggested = re.sub(r"[^A-Za-z0-9]+", "_", header).strip("_").lower() or "optional_column"
            output_name = prompt_text("Output column name", default=suggested)
            optional_columns.append({"output_name": output_name, "header_text": header})

    sku_examples = prompt_list(
        "Positive product-code example",
        minimum=3,
        help_text="Provide at least 3 examples from this catalogue. Examples can include punctuation.",
    )
    negative_examples = prompt_list(
        "Optional negative/non-product example",
        minimum=0,
        help_text="Optional: provide examples that should NOT be treated as SKUs, such as section headers.",
    )

    suggested_sku_regex = suggest_sku_regex(sku_examples)
    print("\nSuggested SKU regex:")
    print(suggested_sku_regex)
    if prompt_yes_no("Use this suggested SKU regex?", default=True):
        sku_regex = suggested_sku_regex
    else:
        sku_regex = prompt_text("Enter SKU regex")
    compile_regex(sku_regex, "SKU")

    page_examples = prompt_list(
        "Catalogue page-value example",
        minimum=1,
        help_text="Provide examples from the page column, e.g. 456, A12, 12/13, See page 12.",
    )
    suggested_page_regex = suggest_page_regex(page_examples)
    print("\nSuggested page-value regex:")
    print(suggested_page_regex)
    if prompt_yes_no("Use this suggested page-value regex?", default=True):
        page_regex = suggested_page_regex
    else:
        page_regex = prompt_text("Enter page-value regex, or blank to keep any non-empty page-column text", required=False)
    if page_regex:
        compile_regex(page_regex, "page")

    example_pairs = []
    if prompt_yes_no("Do you want to enter known SKU/page pairs for validation?", default=True):
        print("Enter each known pair as: SKU|PAGE. Blank to finish.")
        while True:
            pair = prompt_text("Known pair", required=False)
            if not pair:
                break
            if "|" not in pair:
                print("Please use SKU|PAGE format.")
                continue
            sku, page = pair.split("|", 1)
            example_pairs.append({"sku": collapse_ws(sku), "page": collapse_ws(page)})

    extraction_mode = "auto"
    if prompt_yes_no("Use manual coordinate mode instead of automatic header detection?", default=False):
        extraction_mode = "manual"

    config: Dict[str, Any] = {
        "input_pdf": input_pdf,
        "output_folder": output_folder,
        "index_pdf_pages": index_pdf_pages,
        "page_source_of_truth": "pdf_page_number",
        "required_columns": {
            "sku": {"header_text": sku_header},
            "page": {"header_text": page_header},
        },
        "optional_columns": optional_columns,
        "expected_table_blocks_per_page": expected_blocks,
        "header_matching": {"case_sensitive": case_sensitive},
        "sku_detection": {
            "positive_examples": sku_examples,
            "negative_examples": negative_examples,
            "sku_regex": sku_regex,
        },
        "page_detection": {
            "positive_examples": page_examples,
            "page_regex": page_regex,
            "keep_original_and_normalized": True,
        },
        "example_validation_pairs": example_pairs,
        "extraction_mode": extraction_mode,
        "debug_images": {"enabled": True, "only_issue_pages": True},
        "review_files": {"excel_safe": True},
        "advanced": {
            "header_y_tolerance": 6.0,
            "line_y_tolerance": 3.0,
            "data_start_padding": 1.0,
            "x_tolerance": 1,
            "y_tolerance": 3,
        },
    }

    if extraction_mode == "manual":
        prompt_manual_blocks(config)

    save_path_default = str(Path(output_folder) / "catalogue_index_config.yaml")
    if prompt_yes_no("Save this config file?", default=True):
        save_path = Path(prompt_text("Config save path", default=save_path_default))
        save_config(config, save_path)
        print(f"Saved config: {save_path}")

    return config


# -----------------------------
# Main extraction process
# -----------------------------


def validate_config(config: Dict[str, Any]) -> None:
    required_keys = ["input_pdf", "output_folder", "index_pdf_pages", "required_columns", "sku_detection"]
    missing = [k for k in required_keys if k not in config]
    if missing:
        raise SystemExit(f"Config is missing required key(s): {', '.join(missing)}")
    if "sku" not in config["required_columns"] or "page" not in config["required_columns"]:
        raise SystemExit("Config required_columns must include sku and page.")
    if not config["sku_detection"].get("sku_regex"):
        raise SystemExit("Config sku_detection.sku_regex is required.")
    if len(config["sku_detection"].get("positive_examples", []) or []) < 3:
        raise SystemExit("At least 3 positive SKU examples are required in sku_detection.positive_examples.")


def make_source_catalogue_page(pdf_page: int, config: Dict[str, Any]) -> str:
    # PDF page number is source of truth. This optional offset is only preserved as reference if supplied.
    offset = config.get("catalogue_page_offset")
    if offset in (None, ""):
        return ""
    try:
        return str(int(pdf_page) + int(offset))
    except Exception:
        return ""


def run_extraction(config: Dict[str, Any]) -> Dict[str, Any]:
    validate_config(config)
    input_pdf = Path(config["input_pdf"])
    output_folder = Path(config["output_folder"])
    output_folder.mkdir(parents=True, exist_ok=True)

    pages = parse_page_range(config["index_pdf_pages"])
    sku_regex = compile_regex(config["sku_detection"]["sku_regex"], "SKU")
    page_regex = None
    if config.get("page_detection", {}).get("page_regex"):
        page_regex = compile_regex(config["page_detection"]["page_regex"], "page")

    extraction_mode = config.get("extraction_mode", "auto")
    expected_blocks = int(config.get("expected_table_blocks_per_page", 1))
    advanced = config.get("advanced", {})

    index_rows: List[Dict[str, Any]] = []
    page_diagnostics: List[Dict[str, Any]] = []
    header_detection: List[Dict[str, Any]] = []
    raw_text_audit: List[Dict[str, Any]] = []
    unresolved_rows: List[Dict[str, Any]] = []
    issue_pages = set()
    debug_page_data: Dict[int, List[TableBlock]] = defaultdict(list)
    extractor_errors: List[Dict[str, Any]] = []

    log(f"Opening PDF: {input_pdf}")
    with pdfplumber.open(str(input_pdf)) as pdf:
        total_pages = len(pdf.pages)
        log(f"PDF has {total_pages} pages. Processing {len(pages)} configured index pages.")
        for idx, pdf_page in enumerate(pages, start=1):
            start_time = time.perf_counter()
            diag_issues: List[str] = []
            if pdf_page < 1 or pdf_page > total_pages:
                msg = f"configured_pdf_page_out_of_range:{pdf_page}"
                extractor_errors.append({"source_pdf_page": pdf_page, "error": msg})
                issue_pages.add(pdf_page)
                continue

            log(f"Page {idx}/{len(pages)}: PDF page {pdf_page} starting")
            page = pdf.pages[pdf_page - 1]
            try:
                words = page.extract_words(
                    x_tolerance=advanced.get("x_tolerance", 1),
                    y_tolerance=advanced.get("y_tolerance", 3),
                    keep_blank_chars=False,
                    use_text_flow=False,
                )
            except Exception as exc:
                extractor_errors.append({"source_pdf_page": pdf_page, "error": repr(exc)})
                issue_pages.add(pdf_page)
                continue

            # Raw text audit is intentionally independent from the structured coordinate extraction.
            page_raw_audit = raw_text_sku_audit_for_page(page, pdf_page, sku_regex)
            raw_text_audit.extend(page_raw_audit)

            try:
                if extraction_mode == "manual":
                    blocks = manual_blocks_from_config(float(page.width), pdf_page, config)
                    page_header_records: List[Dict[str, Any]] = []
                else:
                    blocks, page_header_records = detect_auto_blocks(float(page.width), pdf_page, words, config)
                    header_detection.extend(page_header_records)
            except Exception as exc:
                extractor_errors.append({"source_pdf_page": pdf_page, "error": f"block_detection_error:{repr(exc)}"})
                issue_pages.add(pdf_page)
                continue

            debug_page_data[pdf_page].extend(blocks)

            if len(blocks) != expected_blocks:
                diag_issues.append(f"expected_blocks={expected_blocks};detected_blocks={len(blocks)}")

            page_row_count = 0
            for block in blocks:
                rows, block_issues = extract_rows_from_block(
                    pdf_page=pdf_page,
                    page_height=float(page.height),
                    words=words,
                    block=block,
                    config=config,
                    sku_regex=sku_regex,
                    page_regex=page_regex,
                )
                for row in rows:
                    row["source_catalogue_page"] = make_source_catalogue_page(pdf_page, config)
                    index_rows.append(row)
                    page_row_count += 1
                    if row.get("confidence_status") == "needs_review":
                        unresolved_rows.append(row)
                if block_issues:
                    diag_issues.extend([f"block_{block.block_number}:{issue}" for issue in block_issues])

            structured_norms_on_page = {r["sku_normalized"] for r in index_rows if r.get("source_pdf_page") == pdf_page and r.get("sku_normalized")}
            raw_norms_on_page = {r["sku_normalized"] for r in page_raw_audit if r.get("sku_normalized")}
            raw_missing_on_page = sorted(raw_norms_on_page - structured_norms_on_page)
            if raw_missing_on_page:
                diag_issues.append(f"raw_text_skus_not_in_structured:{len(raw_missing_on_page)}")

            elapsed = time.perf_counter() - start_time
            page_diag = {
                "source_pdf_page": pdf_page,
                "source_catalogue_page": make_source_catalogue_page(pdf_page, config),
                "expected_table_blocks": expected_blocks,
                "detected_table_blocks": len(blocks),
                "structured_rows": page_row_count,
                "raw_text_sku_candidates": len(page_raw_audit),
                "raw_text_unique_sku_candidates": len(raw_norms_on_page),
                "raw_text_skus_not_in_structured": len(raw_missing_on_page),
                "issue_count": len(diag_issues),
                "issues": ";".join(dedupe_preserve(diag_issues)),
                "elapsed_seconds": round(elapsed, 3),
            }
            page_diagnostics.append(page_diag)
            if diag_issues:
                issue_pages.add(pdf_page)
            log(
                f"Page {idx}/{len(pages)}: PDF page {pdf_page} done - "
                f"blocks={len(blocks)}, rows={page_row_count}, raw_skus={len(page_raw_audit)}, "
                f"issues={len(diag_issues)}, {elapsed:.2f}s"
            )

        # Add raw-text-only candidates to index_rows after the full structured pass.
        structured_norms = {r["sku_normalized"] for r in index_rows if r.get("sku_normalized")}
        for audit_row in raw_text_audit:
            norm = audit_row.get("sku_normalized", "")
            audit_row["in_structured_output_before_raw_add"] = norm in structured_norms
            if norm and norm not in structured_norms:
                audit_row["added_to_registry_from_raw_text"] = True
                raw_only_row = {
                    "sku": audit_row["sku"],
                    "sku_normalized": norm,
                    "catalogue_page_original": "",
                    "catalogue_page_normalized": "",
                    "source_pdf_page": audit_row["source_pdf_page"],
                    "source_catalogue_page": make_source_catalogue_page(int(audit_row["source_pdf_page"]), config),
                    "source_table_block": "",
                    "source_method": "raw_text_audit",
                    "detection_mode": "raw_text_only",
                    "sku_top": "",
                    "page_top": "",
                    "confidence_status": "needs_review",
                    "review_reason": "raw_text_candidate_not_in_structured_output",
                    "source_sku_line_text": audit_row.get("context", ""),
                    "source_page_line_text": "",
                }
                for col in config.get("optional_columns", []) or []:
                    name = col["output_name"]
                    raw_only_row[name] = ""
                    raw_only_row[f"{name}_confidence"] = "missing"
                index_rows.append(raw_only_row)
                unresolved_rows.append(raw_only_row)
                structured_norms.add(norm)
                issue_pages.add(int(audit_row["source_pdf_page"]))

        # Recompute raw audit flags after additions.
        final_norms = {r["sku_normalized"] for r in index_rows if r.get("sku_normalized")}
        for audit_row in raw_text_audit:
            audit_row["in_final_registry"] = audit_row.get("sku_normalized", "") in final_norms

        sku_registry = build_sku_registry(index_rows, config)
        example_validation = validate_example_pairs(sku_registry, config)
        for ev in example_validation:
            if ev["status"] != "found":
                # Page unknown for missing pair, but still mark debug generation if the SKU appears in raw audit.
                for audit_row in raw_text_audit:
                    if audit_row.get("sku_normalized") == ev.get("sku_normalized"):
                        issue_pages.add(int(audit_row["source_pdf_page"]))

    outputs = {
        "index_rows": index_rows,
        "sku_registry": sku_registry,
        "unresolved_rows": unresolved_rows,
        "page_diagnostics": page_diagnostics,
        "header_detection": header_detection,
        "raw_text_sku_audit": raw_text_audit,
        "example_validation": example_validation,
        "extractor_errors": extractor_errors,
    }

    write_outputs(outputs, output_folder, config)

    if config.get("debug_images", {}).get("enabled", True):
        generate_debug_images(input_pdf, output_folder, sorted(issue_pages), debug_page_data, config)

    log("Extraction complete.")
    log(f"Main output: {output_folder / 'sku_registry.csv'}")
    return outputs


# -----------------------------
# Registry, validation, outputs
# -----------------------------


def build_sku_registry(index_rows: List[Dict[str, Any]], config: Dict[str, Any]) -> List[Dict[str, Any]]:
    by_sku: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in index_rows:
        norm = row.get("sku_normalized", "")
        if norm:
            by_sku[norm].append(row)

    registry: List[Dict[str, Any]] = []
    optional_columns = config.get("optional_columns", []) or []
    for norm, rows in sorted(by_sku.items(), key=lambda item: item[0]):
        first_sku = next((r.get("sku", "") for r in rows if r.get("sku")), norm)
        pages_original = dedupe_preserve(r.get("catalogue_page_original", "") for r in rows)
        pages_norm = dedupe_preserve(r.get("catalogue_page_normalized", "") for r in rows)
        source_pdf_pages = dedupe_preserve(str(r.get("source_pdf_page", "")) for r in rows)
        source_catalogue_pages = dedupe_preserve(str(r.get("source_catalogue_page", "")) for r in rows)
        source_table_blocks = dedupe_preserve(str(r.get("source_table_block", "")) for r in rows)
        methods = dedupe_preserve(str(r.get("source_method", "")) for r in rows)
        reasons = dedupe_preserve(
            part
            for r in rows
            for part in str(r.get("review_reason", "")).split(";")
            if part
        )
        confidence = "needs_review" if any(r.get("confidence_status") == "needs_review" for r in rows) else "confirmed"

        reg: Dict[str, Any] = {
            "sku": first_sku,
            "sku_normalized": norm,
            "catalogue_pages_original": ";".join(pages_original),
            "catalogue_pages_normalized": ";".join(pages_norm),
            "source_pdf_pages": ";".join(source_pdf_pages),
            "source_catalogue_pages": ";".join(source_catalogue_pages),
            "source_table_blocks": ";".join(source_table_blocks),
            "source_methods": ";".join(methods),
            "confidence_status": confidence,
            "review_reason": ";".join(reasons),
            "occurrence_count": len(rows),
        }

        for col in optional_columns:
            name = col["output_name"]
            reg[name] = ";".join(dedupe_preserve(r.get(name, "") for r in rows))
            reg[f"{name}_confidence"] = "needs_review" if any(r.get(f"{name}_confidence") == "needs_review" for r in rows) else "confirmed"

        registry.append(reg)
    return registry


def validate_example_pairs(sku_registry: List[Dict[str, Any]], config: Dict[str, Any]) -> List[Dict[str, Any]]:
    reg_by_norm = {r["sku_normalized"]: r for r in sku_registry}
    results: List[Dict[str, Any]] = []
    for pair in config.get("example_validation_pairs", []) or []:
        sku = collapse_ws(pair.get("sku", ""))
        page = collapse_ws(pair.get("page", ""))
        sku_norm = normalize_sku(sku)
        page_norm = normalize_page_value(page)
        reg = reg_by_norm.get(sku_norm)
        if not reg:
            status = "sku_not_found"
            reason = "example_sku_missing_from_registry"
        else:
            pages = set(str(reg.get("catalogue_pages_normalized", "")).split(";"))
            if page_norm in pages:
                status = "found"
                reason = ""
            else:
                status = "page_not_found_for_sku"
                reason = "example_page_missing_for_sku"
        results.append(
            {
                "example_sku": sku,
                "sku_normalized": sku_norm,
                "example_page_original": page,
                "example_page_normalized": page_norm,
                "status": status,
                "review_reason": reason,
            }
        )
    return results


def fieldnames_for_rows(rows: List[Dict[str, Any]], preferred: Optional[List[str]] = None) -> List[str]:
    seen = set()
    fields: List[str] = []
    for f in preferred or []:
        if f not in seen:
            seen.add(f)
            fields.append(f)
    for row in rows:
        for f in row.keys():
            if f not in seen:
                seen.add(f)
                fields.append(f)
    return fields


def write_csv(path: Path, rows: List[Dict[str, Any]], preferred_fields: Optional[List[str]] = None, excel_safe: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = fieldnames_for_rows(rows, preferred_fields)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            if excel_safe:
                writer.writerow({k: safe_review_value(row.get(k, "")) for k in fields})
            else:
                writer.writerow({k: row.get(k, "") for k in fields})


def write_outputs(outputs: Dict[str, Any], output_folder: Path, config: Dict[str, Any]) -> None:
    excel_safe_review = bool(config.get("review_files", {}).get("excel_safe", True))

    write_csv(
        output_folder / "sku_registry.csv",
        outputs["sku_registry"],
        preferred_fields=[
            "sku",
            "sku_normalized",
            "catalogue_pages_original",
            "catalogue_pages_normalized",
            "source_pdf_pages",
            "source_catalogue_pages",
            "source_table_blocks",
            "source_methods",
            "confidence_status",
            "review_reason",
            "occurrence_count",
        ],
        excel_safe=False,
    )
    write_csv(output_folder / "index_rows_review.csv", outputs["index_rows"], excel_safe=excel_safe_review)
    write_csv(output_folder / "unresolved_rows_review.csv", outputs["unresolved_rows"], excel_safe=excel_safe_review)
    write_csv(output_folder / "page_diagnostics_review.csv", outputs["page_diagnostics"], excel_safe=excel_safe_review)
    write_csv(output_folder / "raw_text_sku_audit_review.csv", outputs["raw_text_sku_audit"], excel_safe=excel_safe_review)
    write_csv(output_folder / "header_detection_review.csv", outputs["header_detection"], excel_safe=excel_safe_review)
    write_csv(output_folder / "example_validation_review.csv", outputs["example_validation"], excel_safe=excel_safe_review)
    write_csv(output_folder / "extractor_errors_review.csv", outputs["extractor_errors"], excel_safe=excel_safe_review)

    summary = [
        {"metric": "sku_registry_rows", "value": len(outputs["sku_registry"])},
        {"metric": "index_row_occurrences", "value": len(outputs["index_rows"])},
        {"metric": "unresolved_rows", "value": len(outputs["unresolved_rows"])},
        {"metric": "pages_with_issues", "value": sum(1 for r in outputs["page_diagnostics"] if int(r.get("issue_count", 0) or 0) > 0)},
        {"metric": "raw_text_sku_occurrences", "value": len(outputs["raw_text_sku_audit"])},
        {
            "metric": "raw_text_skus_added_to_registry",
            "value": sum(1 for r in outputs["raw_text_sku_audit"] if r.get("added_to_registry_from_raw_text")),
        },
        {"metric": "extractor_errors", "value": len(outputs["extractor_errors"])},
    ]
    write_csv(output_folder / "run_summary_review.csv", summary, excel_safe=excel_safe_review)


# -----------------------------
# Debug image generation
# -----------------------------


def generate_debug_images(
    input_pdf: Path,
    output_folder: Path,
    issue_pages: List[int],
    debug_page_data: Dict[int, List[TableBlock]],
    config: Dict[str, Any],
) -> None:
    if not issue_pages:
        log("No issue pages found, so no debug images generated.")
        return
    debug_folder = output_folder / "debug_images"
    debug_folder.mkdir(parents=True, exist_ok=True)
    log(f"Generating debug images for {len(issue_pages)} issue page(s).")

    try:
        with pdfplumber.open(str(input_pdf)) as pdf:
            for pdf_page in issue_pages:
                if pdf_page < 1 or pdf_page > len(pdf.pages):
                    continue
                page = pdf.pages[pdf_page - 1]
                try:
                    im = page.to_image(resolution=120)
                    for block in debug_page_data.get(pdf_page, []):
                        # Draw block boundary and column zones. pdfplumber expects PDF coords.
                        im.draw_rect((block.block_x0, 0, block.block_x1, float(page.height)), stroke="red", stroke_width=1)
                        for field_name, zone in block.column_zones.items():
                            im.draw_rect((zone.x0, block.header_bottom, zone.x1, float(page.height)), stroke="blue", stroke_width=1)
                    out_path = debug_folder / f"debug_pdf_page_{pdf_page}.png"
                    im.save(str(out_path), format="PNG")
                except Exception as exc:
                    log(f"Could not generate debug image for PDF page {pdf_page}: {exc}")
    except Exception as exc:
        log(f"Could not open PDF for debug image generation: {exc}")


# -----------------------------
# CLI
# -----------------------------


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Extract SKU/page registry from selectable-text PDF catalogue index pages.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent(
            """
            Examples
            --------
            Interactive setup:
              py catalogue_index_extractor.py --interactive

            Run from saved config:
              py catalogue_index_extractor.py --config catalogue_index_config.yaml
            """
        ),
    )
    parser.add_argument("--config", type=str, help="Path to YAML or JSON config file.")
    parser.add_argument("--interactive", action="store_true", help="Prompt for settings interactively and optionally save config.")
    parser.add_argument("--input", type=str, help="Override input_pdf from config.")
    parser.add_argument("--output", type=str, help="Override output_folder from config.")
    parser.add_argument("--pages", type=str, help="Override index_pdf_pages from config.")
    parser.add_argument("--manual-coordinates", action="store_true", help="Prompt for manual coordinate blocks before running.")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> None:
    args = parse_args(argv)

    if args.interactive or not args.config:
        config = build_interactive_config()
    else:
        config = load_config(Path(args.config))

    if args.input:
        config["input_pdf"] = args.input
    if args.output:
        config["output_folder"] = args.output
    if args.pages:
        config["index_pdf_pages"] = args.pages
    if args.manual_coordinates:
        config["extraction_mode"] = "manual"
        prompt_manual_blocks(config)

    # Preserve the exact config used for the run in the output folder.
    output_folder = Path(config["output_folder"])
    output_folder.mkdir(parents=True, exist_ok=True)
    save_config(config, output_folder / "catalogue_index_config_used.yaml")

    run_extraction(config)


if __name__ == "__main__":
    main()
