"""SKU-anchored, catalogue-agnostic table interpretation.

Version 9 changes the unit of reasoning from the whole table to each confirmed
or strongly plausible SKU anchor.  PyMuPDF4LLM may propose a table region, but
this module independently compares compatible PyMuPDF line-grid candidates and
selects the representation that preserves SKU cells most faithfully.

The module separates:

* faithful reconstructed table output;
* SKU anchors and header paths;
* all candidate SKU-to-attribute relationships;
* accepted high-precision relationships; and
* one-record-per-SKU normalized output.

No manufacturer is hardcoded. Optional JSON profiles supply reusable SKU
patterns, header aliases, option-code exclusions, units and footnote rules.
"""
from __future__ import annotations

import csv
import json
import math
import re
import statistics
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

import pymupdf

VERSION = "9.0"


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


@dataclass
class HeaderLeaf:
    column_index: int
    components: list[str]
    source_path: str
    normalized_path: str
    bbox: list[float] | None
    semantic_fields: dict[str, str] = field(default_factory=dict)


@dataclass
class TableAnalysis:
    page: int
    table: int
    bbox: list[float]
    header_rows: int
    leaf_headers: list[HeaderLeaf]
    reconstructed_columns: list[str]
    reconstructed_rows: list[list[str]]
    classification: dict[str, Any]
    product_records: list[dict[str, Any]]
    validation: dict[str, Any]
    footnotes: dict[str, str]
    row_models: list[dict[str, Any]]
    cell_graph: dict[str, Any]
    sku_anchors: list[dict[str, Any]]
    header_paths: list[dict[str, Any]]
    attribute_candidates: list[dict[str, Any]]
    attribute_relationships: list[dict[str, Any]]
    extraction_selection: dict[str, Any]


@dataclass
class MatrixCandidate:
    name: str
    rows_raw: list[list[Any]]
    rows: list[list[str]]
    cells: list[list[list[float] | None]]
    bbox: list[float]
    nulls: list[list[bool]]
    score: float = 0.0
    diagnostics: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Generic configuration and helpers
# ---------------------------------------------------------------------------


DEFAULT_PROFILE: dict[str, Any] = {
    "sku_patterns": [
        r"\bGW\s*\d{2}\s*\d{3}(?:\s*[A-Z]{1,4})?\b",
        r"\b[A-Z]{2,8}[A-Z0-9._/-]*\d[A-Z0-9._/-]*\b",
    ],
    "header_aliases": {
        "rated current": "rated_current",
        "rated current (in)": "rated_current",
        "current": "rated_current",
        "rated voltage": "rated_voltage",
        "rated voltage (un)": "rated_voltage",
        "voltage": "rated_voltage",
        "frequency": "frequency",
        "poles": "poles",
        "no. of poles": "poles",
        "reference h": "reference_h",
        "applications": "applications",
        "application": "applications",
        "type": "product_type",
        "colour": "colour",
        "color": "colour",
        "length": "length",
        "width": "width",
        "height/depth": "height_depth",
        "height / depth": "height_depth",
        "diameter": "diameter",
        "pack carton": "pack_carton",
        "pack/carton": "pack_carton",
    },
    "option_codes": [
        "E", "STE", "SEN", "DD", "AD", "STD", "NEW", "IP", "AC", "DC",
    ],
    "footnote_rules": [
        {
            "pattern": r"(?i)phase\s+inverter",
            "attribute": "phase_inverter",
            "value": "Yes",
        },
    ],
    "unit_patterns": {
        "A": r"(?i)(?:^|\s)(A)(?:$|\s)",
        "V": r"(?i)(?:^|\s)(V)(?:$|\s)",
        "Hz": r"(?i)(Hz)",
        "mm": r"(?i)(mm)",
        "W": r"(?i)(?:^|\s)(W)(?:$|\s)",
        "lm": r"(?i)(lm)",
        "K": r"(?i)(?:\d)(K)(?:$|\s)",
    },
    "semantic_component_rules": [
        {"pattern": r"(?i)^IP\s*\d", "attribute": "ip_rating"},
        {"pattern": r"(?i)\b(?:fast|screw|spring|tool[- ]?free)\s+wiring\b", "attribute": "wiring_type"},
        {"pattern": r"(?i)^applications?$", "attribute": "applications"},
    ],
    "auto_accept_confidence": 0.90,
    "flag_confidence": 0.70,
}


def deep_merge_profile(base: dict[str, Any], override: dict[str, Any] | None) -> dict[str, Any]:
    result = json.loads(json.dumps(base))
    if not override:
        return result
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key].update(value)
        elif isinstance(value, list) and isinstance(result.get(key), list):
            result[key] = [*result[key], *value]
        else:
            result[key] = value
    return result


def load_profile(path: Path | str | None) -> dict[str, Any]:
    if path is None:
        return deep_merge_profile(DEFAULT_PROFILE, None)
    value = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError("The table profile must be a JSON object.")
    return deep_merge_profile(DEFAULT_PROFILE, value)


def clean(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).replace("\u00ad", "").replace("\x00", "")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return "\n".join(re.sub(r"[ \t]+", " ", line).strip() for line in text.split("\n")).strip()


def one_line(value: Any) -> str:
    return re.sub(r"\s+", " ", clean(value).replace("\n", " ")).strip()


def slug(value: str, fallback: str = "attribute") -> str:
    result = re.sub(r"[^a-z0-9]+", "_", one_line(value).casefold()).strip("_")
    return result or fallback


def normalize_code(value: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "", one_line(value).upper())


def normalize_bbox(value: Sequence[float]) -> list[float]:
    return [float(value[0]), float(value[1]), float(value[2]), float(value[3])]


def bbox_area(value: Sequence[float]) -> float:
    return max(0.0, float(value[2]) - float(value[0])) * max(0.0, float(value[3]) - float(value[1]))


def bbox_intersection(a: Sequence[float], b: Sequence[float]) -> float:
    return max(0.0, min(float(a[2]), float(b[2])) - max(float(a[0]), float(b[0]))) * max(
        0.0, min(float(a[3]), float(b[3])) - max(float(a[1]), float(b[1]))
    )


def overlap_ratio(a: Sequence[float], b: Sequence[float]) -> float:
    return bbox_intersection(a, b) / max(1.0, min(bbox_area(a), bbox_area(b)))


def rect_union(rects: Iterable[Sequence[float] | None]) -> list[float] | None:
    valid = [normalize_bbox(rect) for rect in rects if rect and len(rect) == 4]
    if not valid:
        return None
    return [
        min(rect[0] for rect in valid), min(rect[1] for rect in valid),
        max(rect[2] for rect in valid), max(rect[3] for rect in valid),
    ]


def write_csv(path: Path, rows: Sequence[Sequence[Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        for row in rows:
            writer.writerow(["" if value is None else value for value in row])


def write_dict_csv(path: Path, rows: Sequence[dict[str, Any]], columns: Sequence[str] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    materialized = list(rows)
    if columns is None:
        columns = []
        seen: set[str] = set()
        for row in materialized:
            for key in row:
                if key not in seen:
                    seen.add(key)
                    columns.append(key)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(columns), extrasaction="ignore")
        writer.writeheader()
        for row in materialized:
            serialised = {
                key: json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else value
                for key, value in row.items()
            }
            writer.writerow(serialised)


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False), encoding="utf-8")


def compile_sku_patterns(profile: dict[str, Any]) -> list[re.Pattern[str]]:
    result: list[re.Pattern[str]] = []
    for value in profile.get("sku_patterns", []):
        try:
            result.append(re.compile(str(value)))
        except re.error:
            continue
    return result


def parse_value(value: str, profile: dict[str, Any]) -> dict[str, str]:
    raw = one_line(value)
    unit = ""
    for candidate, pattern in profile.get("unit_patterns", {}).items():
        try:
            if re.search(str(pattern), raw):
                unit = str(candidate)
                break
        except re.error:
            continue
    if not raw:
        datatype = "empty"
    elif re.fullmatch(r"[-+]?\d+(?:[.,]\d+)?", raw):
        datatype = "number"
    elif re.search(r"\d", raw):
        datatype = "measurement" if unit else "alphanumeric"
    else:
        datatype = "text"
    normalized = raw
    if datatype == "number":
        normalized = raw.replace(",", ".")
    return {"value_raw": raw, "value_normalized": normalized, "unit": unit, "datatype": datatype}


def normalize_attribute_name(source: str, profile: dict[str, Any]) -> str:
    text = one_line(source)
    lowered = text.casefold()
    aliases = {one_line(str(k)).casefold(): str(v) for k, v in profile.get("header_aliases", {}).items()}
    if lowered in aliases:
        return aliases[lowered]
    # Prefer the final header component when a hierarchy exists.
    components = [one_line(item) for item in text.split("|") if one_line(item)]
    for component in reversed(components):
        key = component.casefold()
        if key in aliases:
            return aliases[key]
    return slug(text)


def semantic_fields_from_components(components: Sequence[str], profile: dict[str, Any]) -> dict[str, str]:
    result: dict[str, str] = {}
    aliases = {one_line(str(k)).casefold(): str(v) for k, v in profile.get("header_aliases", {}).items()}
    for component in components:
        component = one_line(component)
        if not component:
            continue
        key = component.casefold()
        alias = aliases.get(key)
        if alias in {"applications", "colour", "length", "width", "height_depth", "diameter", "rated_current", "rated_voltage", "frequency", "poles", "reference_h"}:
            continue
        for rule in profile.get("semantic_component_rules", []):
            try:
                if re.search(str(rule.get("pattern", "")), component):
                    result[str(rule.get("attribute", "attribute"))] = component
            except re.error:
                continue
        if "product_type" not in result:
            if re.search(r"(?i)^IP\s*\d", component):
                continue
            if re.search(r"(?i)\b(?:fast|screw|spring|tool[- ]?free)\s+wiring\b", component):
                continue
            if key in aliases:
                continue
            result["product_type"] = component
    return result


# ---------------------------------------------------------------------------
# SKU detection
# ---------------------------------------------------------------------------


def codes_in_value(
    value: str,
    registry: dict[str, dict[str, Any]],
    patterns: Sequence[re.Pattern[str]],
    option_codes: set[str],
) -> list[dict[str, Any]]:
    text = one_line(value)
    if not text:
        return []
    found: list[dict[str, Any]] = []
    seen: set[str] = set()
    tokens = re.findall(r"[A-Za-z0-9][A-Za-z0-9._/+()\-*]*", text)

    # Canonical registry matches are authoritative and can cross spaces.
    for size in range(min(8, len(tokens)), 0, -1):
        for start in range(len(tokens) - size + 1):
            raw = " ".join(tokens[start:start + size])
            key = normalize_code(raw)
            if key in registry and key not in seen:
                entry = registry[key]
                found.append({
                    "code": str(entry.get("code") or entry.get("sku") or raw),
                    "raw": raw,
                    "normalized": key,
                    "registry_match": "exact_normalized",
                    "unexpected": False,
                    "registry_status": entry.get("confidence_status") or entry.get("registry_status") or "registered",
                })
                seen.add(key)

    # Pattern matches can create unconfirmed anchors, never fuzzy confirmation.
    for pattern in patterns:
        for match in pattern.finditer(text):
            raw = one_line(match.group(0)).rstrip(".,;:")
            key = normalize_code(raw)
            if not key or key in seen or key in option_codes:
                continue
            if re.fullmatch(r"\d+(?:X\d+)?[A-Z]?", key):
                continue
            if re.fullmatch(r"\d+(?:A|V|HZ|W|MM|K)", key):
                continue
            if re.fullmatch(r"IP\d+(?:IP\d+)*", key):
                continue
            if re.fullmatch(r"(?:IP)?\d+(?:IP|V|A|HZ|W|MM|K)\d*", key):
                continue
            found.append({
                "code": raw,
                "raw": raw,
                "normalized": key,
                "registry_match": "unconfirmed_pattern",
                "unexpected": True,
                "registry_status": "unconfirmed",
            })
            seen.add(key)
    return found


# ---------------------------------------------------------------------------
# Matrix candidate selection and merged-cell reconstruction
# ---------------------------------------------------------------------------


def materialize_matrix(rows: Sequence[Sequence[Any]], cells: Sequence[Sequence[Sequence[float] | None]]) -> tuple[list[list[Any]], list[list[list[float] | None]]]:
    width = max(max((len(row) for row in rows), default=0), max((len(row) for row in cells), default=0))
    raw = [list(row) + [None] * (width - len(row)) for row in rows]
    rects = [
        [normalize_bbox(cell) if cell else None for cell in list(row) + [None] * (width - len(row))]
        for row in cells
    ]
    while len(rects) < len(raw):
        rects.append([None] * width)
    return raw, rects


def _row_intervals(cells: Sequence[Sequence[Sequence[float] | None]], bbox: Sequence[float]) -> list[tuple[float, float]]:
    result: list[tuple[float, float]] = []
    height = (float(bbox[3]) - float(bbox[1])) / max(1, len(cells))
    for index, row in enumerate(cells):
        values = [cell for cell in row if cell]
        if values:
            result.append((min(float(cell[1]) for cell in values), max(float(cell[3]) for cell in values)))
        else:
            result.append((float(bbox[1]) + height * index, float(bbox[1]) + height * (index + 1)))
    return result


def _column_intervals(cells: Sequence[Sequence[Sequence[float] | None]], bbox: Sequence[float], width: int) -> list[tuple[float, float]]:
    equal = (float(bbox[2]) - float(bbox[0])) / max(1, width)
    result: list[tuple[float, float]] = []
    for column in range(width):
        x0s: list[float] = []
        x1s: list[float] = []
        for row in cells:
            if column < len(row) and row[column]:
                rect = row[column]
                x0s.append(float(rect[0]))
                x1s.append(float(rect[2]))
        result.append((
            statistics.median(x0s) if x0s else float(bbox[0]) + equal * column,
            statistics.median(x1s) if x1s else float(bbox[0]) + equal * (column + 1),
        ))
    # Convert overlapping span medians into monotonic leaf intervals using all
    # distinct vertical boundaries when available.
    boundaries = sorted({round(float(cell[0]), 3) for row in cells for cell in row if cell} | {round(float(cell[2]), 3) for row in cells for cell in row if cell})
    if len(boundaries) >= width + 1:
        # Choose the boundary sequence with the strongest support for width
        # leaves. The outermost width+1 boundaries work for ruled tables.
        if len(boundaries) == width + 1:
            result = [(boundaries[i], boundaries[i + 1]) for i in range(width)]
    return result


def expand_merged_matrix(
    rows: Sequence[Sequence[Any]],
    cells: Sequence[Sequence[Sequence[float] | None]],
    bbox: Sequence[float],
) -> tuple[list[list[str]], list[list[bool]], list[dict[str, Any]]]:
    raw, rects = materialize_matrix(rows, cells)
    height = len(raw)
    width = max((len(row) for row in raw), default=0)
    row_intervals = _row_intervals(rects, bbox)
    col_intervals = _column_intervals(rects, bbox, width)
    result = [[clean(value) for value in row] for row in raw]
    nulls = [[value is None for value in row] for row in raw]
    events: list[dict[str, Any]] = []

    sources: list[tuple[int, int, list[float], str]] = []
    for r in range(height):
        for c in range(width):
            if rects[r][c] and result[r][c]:
                sources.append((r, c, rects[r][c], result[r][c]))

    for r in range(height):
        row_centre = sum(row_intervals[r]) / 2
        for c in range(width):
            if not nulls[r][c] or result[r][c]:
                continue
            col_centre = sum(col_intervals[c]) / 2
            covering = [
                source for source in sources
                if source[2][0] - 0.75 <= col_centre <= source[2][2] + 0.75
                and source[2][1] - 0.75 <= row_centre <= source[2][3] + 0.75
            ]
            if not covering:
                continue
            # Prefer the smallest covering cell: it is the most specific span.
            source = min(covering, key=lambda item: bbox_area(item[2]))
            result[r][c] = source[3]
            events.append({
                "target_row": r + 1,
                "target_column": c + 1,
                "source_row": source[0] + 1,
                "source_column": source[1] + 1,
                "value": source[3],
                "relationship": "covered_by_merged_cell",
            })
    return result, nulls, events


def _candidate_from_table(table: Any, name: str) -> MatrixCandidate:
    extracted = table.extract()
    rows_raw = [list(row) for row in extracted]
    cells = [[normalize_bbox(cell) if cell else None for cell in row.cells] for row in table.rows]
    rows, nulls, events = expand_merged_matrix(rows_raw, cells, table.bbox)
    return MatrixCandidate(
        name=name,
        rows_raw=rows_raw,
        rows=rows,
        cells=cells,
        bbox=normalize_bbox(table.bbox),
        nulls=nulls,
        diagnostics={"merged_expansions": events},
    )


def _candidate_from_provided(
    rows: Sequence[Sequence[Any]], cells: Sequence[Sequence[Sequence[float] | None]], bbox: Sequence[float]
) -> MatrixCandidate:
    rows_raw, rects = materialize_matrix(rows, cells)
    expanded, nulls, events = expand_merged_matrix(rows_raw, rects, bbox)
    return MatrixCandidate(
        name="provided_layout",
        rows_raw=rows_raw,
        rows=expanded,
        cells=rects,
        bbox=normalize_bbox(bbox),
        nulls=nulls,
        diagnostics={"merged_expansions": events},
    )


def find_header_row_count(
    rows: Sequence[Sequence[str]], registry: dict[str, dict[str, Any]], patterns: Sequence[re.Pattern[str]], option_codes: set[str]
) -> int:
    if not rows:
        return 0
    for index, row in enumerate(rows):
        sku_cells = sum(bool(codes_in_value(value, registry, patterns, option_codes)) for value in row)
        if sku_cells:
            return index
    return 1 if len(rows) > 1 else 0


def score_matrix_candidate(
    candidate: MatrixCandidate,
    target_bbox: Sequence[float],
    registry: dict[str, dict[str, Any]],
    patterns: Sequence[re.Pattern[str]],
    option_codes: set[str],
) -> MatrixCandidate:
    header_rows = find_header_row_count(candidate.rows, registry, patterns, option_codes)
    total_codes = 0
    exact_codes = 0
    sku_cells = 0
    multi_code_cells = 0
    header_code_count = 0
    unique_codes: set[str] = set()
    for r, row in enumerate(candidate.rows):
        for value in row:
            codes = codes_in_value(value, registry, patterns, option_codes)
            if not codes:
                continue
            sku_cells += 1
            if len(codes) > 1:
                multi_code_cells += 1
            total_codes += len(codes)
            exact_codes += sum(item.get("registry_match") == "exact_normalized" for item in codes)
            unique_codes.update(item["normalized"] for item in codes)
            if r < header_rows:
                header_code_count += len(codes)
    width = max((len(row) for row in candidate.rows), default=0)
    height = len(candidate.rows)
    overlap = overlap_ratio(candidate.bbox, target_bbox)
    singleton_ratio = (sku_cells - multi_code_cells) / max(1, sku_cells)
    score = 0.0
    score += min(4.0, exact_codes * 0.025)
    score += min(1.5, len(unique_codes) * 0.012)
    score += overlap * 1.5
    score += singleton_ratio * 1.5
    score += min(0.75, width * 0.04)
    score += min(0.5, height * 0.006)
    score -= multi_code_cells * 1.25
    score -= header_code_count * 0.08
    if width <= 1:
        score -= 2.0
    if total_codes == 0:
        score -= 3.0
    candidate.score = round(score, 6)
    candidate.diagnostics.update({
        "header_rows": header_rows,
        "total_codes": total_codes,
        "exact_codes": exact_codes,
        "unique_codes": len(unique_codes),
        "sku_cells": sku_cells,
        "multi_code_cells": multi_code_cells,
        "header_code_count": header_code_count,
        "singleton_sku_cell_ratio": round(singleton_ratio, 4),
        "bbox_overlap": round(overlap, 4),
        "shape": [height, width],
        "score": candidate.score,
    })
    return candidate


def select_matrix_candidate(
    page: pymupdf.Page,
    bbox: Sequence[float],
    provided_rows: Sequence[Sequence[Any]],
    provided_cells: Sequence[Sequence[Sequence[float] | None]],
    registry: dict[str, dict[str, Any]],
    profile: dict[str, Any],
) -> tuple[MatrixCandidate, dict[str, Any]]:
    patterns = compile_sku_patterns(profile)
    option_codes = {normalize_code(value) for value in profile.get("option_codes", [])}
    candidates: list[MatrixCandidate] = [_candidate_from_provided(provided_rows, provided_cells, bbox)]
    clip = pymupdf.Rect(
        max(0.0, float(bbox[0]) - 10), max(0.0, float(bbox[1]) - 10),
        min(page.rect.width, float(bbox[2]) + 10), min(page.rect.height, float(bbox[3]) + 10),
    )
    for strategy in ("lines", "lines_strict"):
        try:
            finder = page.find_tables(strategy=strategy, clip=clip)
        except TypeError:
            finder = page.find_tables(strategy=strategy)
        for index, table in enumerate(finder.tables, start=1):
            if overlap_ratio(table.bbox, bbox) < 0.35:
                continue
            try:
                candidates.append(_candidate_from_table(table, f"pymupdf_{strategy}_{index}"))
            except Exception:
                continue
    for candidate in candidates:
        score_matrix_candidate(candidate, bbox, registry, patterns, option_codes)
    selected = max(candidates, key=lambda candidate: candidate.score)
    selection = {
        "selected": selected.name,
        "selected_score": selected.score,
        "candidates": [
            {"name": candidate.name, "bbox": candidate.bbox, **candidate.diagnostics}
            for candidate in sorted(candidates, key=lambda item: item.score, reverse=True)
        ],
        "reason": "Highest SKU-preservation and structural score.",
    }
    return selected, selection


# ---------------------------------------------------------------------------
# Header hierarchy and column roles
# ---------------------------------------------------------------------------


def cluster_words_into_phrases(words: Sequence[tuple[Any, ...]]) -> list[dict[str, Any]]:
    if not words:
        return []
    lines: list[list[tuple[Any, ...]]] = []
    for word in sorted(words, key=lambda w: ((float(w[1]) + float(w[3])) / 2, float(w[0]))):
        yc = (float(word[1]) + float(word[3])) / 2
        chosen = None
        for line in lines:
            ly = statistics.mean((float(item[1]) + float(item[3])) / 2 for item in line)
            height = statistics.mean(float(item[3]) - float(item[1]) for item in line)
            if abs(yc - ly) <= max(2.0, height * 0.45):
                chosen = line
                break
        if chosen is None:
            chosen = []
            lines.append(chosen)
        chosen.append(word)
    result: list[dict[str, Any]] = []
    for line in lines:
        ordered = sorted(line, key=lambda item: float(item[0]))
        current: list[tuple[Any, ...]] = []
        for word in ordered:
            if not current:
                current = [word]
                continue
            previous = current[-1]
            gap = float(word[0]) - float(previous[2])
            height = statistics.mean(float(item[3]) - float(item[1]) for item in current)
            if gap > max(7.0, height * 1.45):
                result.append(_phrase(current))
                current = [word]
            else:
                current.append(word)
        if current:
            result.append(_phrase(current))
    return sorted(result, key=lambda item: (item["bbox"][1], item["bbox"][0]))


def _phrase(words: Sequence[tuple[Any, ...]]) -> dict[str, Any]:
    return {
        "text": " ".join(str(word[4]) for word in words),
        "bbox": [
            min(float(word[0]) for word in words), min(float(word[1]) for word in words),
            max(float(word[2]) for word in words), max(float(word[3]) for word in words),
        ],
    }


def leaf_intervals(cells: Sequence[Sequence[Sequence[float] | None]], bbox: Sequence[float], width: int) -> list[tuple[float, float]]:
    return _column_intervals(cells, bbox, width)


def header_hierarchy(
    page: pymupdf.Page,
    rows: Sequence[Sequence[str]],
    cells: Sequence[Sequence[Sequence[float] | None]],
    bbox: Sequence[float],
    header_rows: int,
    profile: dict[str, Any],
) -> list[HeaderLeaf]:
    width = max((len(row) for row in rows), default=0)
    intervals = leaf_intervals(cells, bbox, width)
    data_y = float(bbox[1]) + 60
    if header_rows < len(cells):
        first_data = [cell for cell in cells[header_rows] if cell]
        if first_data:
            data_y = min(float(cell[1]) for cell in first_data)
    header_rect = [float(bbox[0]), float(bbox[1]), float(bbox[2]), data_y]
    phrases = cluster_words_into_phrases(page.get_text("words", clip=pymupdf.Rect(header_rect), sort=True))
    components: list[list[tuple[float, str, int]]] = [[] for _ in range(width)]
    for phrase in phrases:
        text = one_line(phrase["text"])
        if not text:
            continue
        px0, py0, px1, _ = phrase["bbox"]
        overlaps: list[int] = []
        for column, (x0, x1) in enumerate(intervals):
            overlap = max(0.0, min(px1, x1) - max(px0, x0))
            if overlap >= min(max(1.0, px1 - px0), max(1.0, x1 - x0)) * 0.12:
                overlaps.append(column)
        if not overlaps:
            centre = (px0 + px1) / 2
            overlaps = [min(range(width), key=lambda index: abs(centre - sum(intervals[index]) / 2))]
        for column in overlaps:
            components[column].append((float(py0), text, len(overlaps)))

    leaves: list[HeaderLeaf] = []
    for column in range(width):
        ordered: list[str] = []
        for _, text, _ in sorted(components[column], key=lambda item: item[0]):
            if text and text.casefold() not in {value.casefold() for value in ordered}:
                ordered.append(text)
        if not ordered:
            for row in rows[:max(1, header_rows)]:
                if column < len(row) and one_line(row[column]):
                    ordered.append(one_line(row[column]))
        path = " | ".join(ordered) if ordered else f"column_{column + 1}"
        leaves.append(HeaderLeaf(
            column_index=column,
            components=ordered,
            source_path=path,
            normalized_path=normalize_attribute_name(path, profile),
            bbox=[intervals[column][0], header_rect[1], intervals[column][1], header_rect[3]],
            semantic_fields=semantic_fields_from_components(ordered, profile),
        ))
    return leaves


def drop_inactive_columns(
    rows: list[list[str]],
    cells: list[list[list[float] | None]],
    nulls: list[list[bool]],
    leaves: list[HeaderLeaf],
    header_rows: int,
    registry: dict[str, dict[str, Any]],
    patterns: Sequence[re.Pattern[str]],
    option_codes: set[str],
) -> tuple[list[list[str]], list[list[list[float] | None]], list[list[bool]], list[HeaderLeaf], list[int]]:
    width = len(leaves)
    keep: list[int] = []
    for column in range(width):
        data_values = [one_line(rows[row][column]) for row in range(header_rows, len(rows))]
        data_nonempty = sum(bool(value) for value in data_values)
        sku_count = sum(bool(codes_in_value(value, registry, patterns, option_codes)) for value in data_values)
        if data_nonempty or sku_count:
            keep.append(column)
    if not keep:
        keep = list(range(width))
    new_rows = [[row[column] for column in keep] for row in rows]
    new_cells = [[row[column] for column in keep] for row in cells]
    new_nulls = [[row[column] for column in keep] for row in nulls]
    new_leaves: list[HeaderLeaf] = []
    for new_index, old_index in enumerate(keep):
        leaf = leaves[old_index]
        new_leaves.append(HeaderLeaf(
            column_index=new_index,
            components=leaf.components,
            source_path=leaf.source_path,
            normalized_path=leaf.normalized_path,
            bbox=leaf.bbox,
            semantic_fields=leaf.semantic_fields,
        ))
    return new_rows, new_cells, new_nulls, new_leaves, [index + 1 for index in range(width) if index not in keep]


def classify_columns(
    rows: Sequence[Sequence[str]], header_rows: int, leaves: Sequence[HeaderLeaf],
    registry: dict[str, dict[str, Any]], patterns: Sequence[re.Pattern[str]], option_codes: set[str], profile: dict[str, Any]
) -> list[dict[str, Any]]:
    data = rows[header_rows:]
    roles: list[dict[str, Any]] = []
    for column, leaf in enumerate(leaves):
        values = [one_line(row[column]) if column < len(row) else "" for row in data]
        nonempty = sum(bool(value) for value in values)
        sku_count = sum(bool(codes_in_value(value, registry, patterns, option_codes)) for value in values)
        density = sku_count / max(1, nonempty)
        header_codes = codes_in_value(leaf.source_path, registry, patterns, option_codes)
        if len(header_codes) >= 1:
            role = "sku_header_column"
        elif sku_count >= 2 and density >= 0.35:
            role = "sku_column"
        elif nonempty:
            role = "attribute_column"
        else:
            role = "inactive"
        roles.append({
            "column": column + 1,
            "role": role,
            "source_header": leaf.source_path,
            "normalized_header": leaf.normalized_path,
            "nonempty_data_cells": nonempty,
            "sku_cells": sku_count,
            "sku_density": round(density, 4),
            "header_skus": [item["code"] for item in header_codes],
        })
    return roles


# ---------------------------------------------------------------------------
# SKU-anchored relationship engine
# ---------------------------------------------------------------------------


def _cell_id(row: int, column: int) -> str:
    return f"r{row:04d}c{column:03d}"


def build_cell_graph(
    rows: Sequence[Sequence[str]], cells: Sequence[Sequence[Sequence[float] | None]], nulls: Sequence[Sequence[bool]],
    header_rows: int, leaves: Sequence[HeaderLeaf], column_roles: Sequence[dict[str, Any]],
    registry: dict[str, dict[str, Any]], patterns: Sequence[re.Pattern[str]], option_codes: set[str], profile: dict[str, Any],
    merged_events: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    inherited_targets = {(event["target_row"], event["target_column"]): event for event in merged_events}
    graph: list[dict[str, Any]] = []
    for r, row in enumerate(rows, start=1):
        for c, value in enumerate(row, start=1):
            text = one_line(value)
            codes = codes_in_value(text, registry, patterns, option_codes)
            rect = cells[r - 1][c - 1] if r - 1 < len(cells) and c - 1 < len(cells[r - 1]) else None
            parsed = parse_value(text, profile)
            graph.append({
                "cell_id": _cell_id(r, c),
                "row": r,
                "column": c,
                "text": text,
                "bbox": rect,
                "header_path": leaves[c - 1].source_path if c - 1 < len(leaves) else f"column_{c}",
                "normalized_header": leaves[c - 1].normalized_path if c - 1 < len(leaves) else f"column_{c}",
                "column_role": column_roles[c - 1]["role"] if c - 1 < len(column_roles) else "unknown",
                "is_header": r <= header_rows,
                "is_sku": bool(codes),
                "sku_codes": [item["code"] for item in codes],
                "sku_match_types": [item["registry_match"] for item in codes],
                "is_numeric": bool(re.search(r"\d", text)),
                "unit": parsed["unit"],
                "datatype": parsed["datatype"],
                "is_empty": not bool(text),
                "is_null_placeholder": bool(nulls[r - 1][c - 1]) if r - 1 < len(nulls) and c - 1 < len(nulls[r - 1]) else False,
                "inherited_from_merged_cell": (r, c) in inherited_targets,
                "merged_source": inherited_targets.get((r, c)),
            })
    return {
        "header_rows": header_rows,
        "row_count": len(rows),
        "column_count": max((len(row) for row in rows), default=0),
        "column_roles": list(column_roles),
        "cells": graph,
    }


def make_sku_anchors(cell_graph: dict[str, Any], registry: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    anchors: list[dict[str, Any]] = []
    counter = 0
    for cell in cell_graph["cells"]:
        for code, match_type in zip(cell.get("sku_codes", []), cell.get("sku_match_types", [])):
            counter += 1
            key = normalize_code(code)
            entry = registry.get(key, {})
            anchors.append({
                "anchor_id": f"sku_{counter:06d}",
                "sku": code,
                "sku_normalized": key,
                "registry_match_type": match_type,
                "registry_status": entry.get("confidence_status") or entry.get("registry_status") or ("unconfirmed" if match_type == "unconfirmed_pattern" else "registered"),
                "row": cell["row"],
                "column": cell["column"],
                "cell_id": cell["cell_id"],
                "bbox": cell.get("bbox"),
                "column_role": cell.get("column_role"),
                "orientation": "column_header_index" if cell.get("is_header") else (
                    "left_row_index" if cell["column"] == 1 else "right_row_index" if cell["column"] == cell_graph["column_count"] else "internal_matrix_cell"
                ),
                "confidence": 1.0 if match_type == "exact_normalized" else 0.75,
            })
    # Several SKU anchors on the same data row are explicitly matrix anchors.
    per_row: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for anchor in anchors:
        if anchor["orientation"] != "column_header_index":
            per_row[int(anchor["row"])].append(anchor)
    for values in per_row.values():
        if len(values) > 1:
            for anchor in values:
                anchor["orientation"] = "multiple_codes_in_row"
    return anchors


def header_paths_rows(leaves: Sequence[HeaderLeaf], column_roles: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for leaf, role in zip(leaves, column_roles):
        if not leaf.components:
            result.append({
                "column": leaf.column_index + 1,
                "header_level": 1,
                "header_text": leaf.source_path,
                "normalized_header": leaf.normalized_path,
                "column_role": role["role"],
                "bbox": leaf.bbox,
                "confidence": 0.60 if leaf.source_path.startswith("column_") else 0.90,
            })
            continue
        for level, component in enumerate(leaf.components, start=1):
            result.append({
                "column": leaf.column_index + 1,
                "header_level": level,
                "header_text": component,
                "normalized_header": normalize_attribute_name(component, DEFAULT_PROFILE),
                "column_role": role["role"],
                "bbox": leaf.bbox,
                "confidence": 0.98,
            })
    return result


def _candidate_row(
    *, anchor: dict[str, Any], source_cell: dict[str, Any] | None, source_attribute: str,
    normalized_attribute: str, value: str, relationship: str, confidence: float,
    decision: str, rejection_reason: str = "", header_path: Sequence[str] | None = None,
    cells_crossed: int = 0, sku_cells_crossed: int = 0, inherited: bool = False,
) -> dict[str, Any]:
    parsed = parse_value(value, DEFAULT_PROFILE)
    return {
        "anchor_id": anchor["anchor_id"],
        "sku": anchor["sku"],
        "sku_normalized": anchor["sku_normalized"],
        "attribute_cell_id": source_cell.get("cell_id") if source_cell else "",
        "source_attribute": source_attribute,
        "normalized_attribute": normalized_attribute,
        "candidate_value": one_line(value),
        "value_normalized": parsed["value_normalized"],
        "unit": parsed["unit"],
        "relationship_direction": relationship,
        "source_row": source_cell.get("row") if source_cell else "",
        "source_column": source_cell.get("column") if source_cell else anchor.get("column"),
        "source_bbox": source_cell.get("bbox") if source_cell else None,
        "header_path": list(header_path or []),
        "cells_crossed": cells_crossed,
        "sku_cells_crossed": sku_cells_crossed,
        "inherited": inherited,
        "confidence": round(float(confidence), 4),
        "decision": decision,
        "rejection_reason": rejection_reason,
    }


def build_attribute_candidates(
    rows: Sequence[Sequence[str]], leaves: Sequence[HeaderLeaf], cell_graph: dict[str, Any],
    anchors: Sequence[dict[str, Any]], profile: dict[str, Any], footnotes: dict[str, str],
) -> list[dict[str, Any]]:
    cells_by_position = {(int(cell["row"]), int(cell["column"])): cell for cell in cell_graph["cells"]}
    role_by_column = {int(role["column"]): role for role in cell_graph["column_roles"]}
    candidates: list[dict[str, Any]] = []
    for anchor in anchors:
        row = int(anchor["row"])
        column = int(anchor["column"])
        leaf = leaves[column - 1]

        # Upward search: hierarchical column headers become separate fields.
        for name, value in leaf.semantic_fields.items():
            candidates.append(_candidate_row(
                anchor=anchor, source_cell=None, source_attribute=name,
                normalized_attribute=name, value=value, relationship="up_header_path",
                confidence=0.99, decision="accepted", header_path=leaf.components,
            ))
        candidates.append(_candidate_row(
            anchor=anchor, source_cell=None, source_attribute="source_column_path",
            normalized_attribute="source_column_path", value=leaf.source_path,
            relationship="up_header_path", confidence=0.99, decision="accepted",
            header_path=leaf.components,
        ))

        # Same-row search across the full matrix. Other SKU columns may be
        # crossed because registry-backed SKU cells are explicitly known.
        for target_column in range(1, cell_graph["column_count"] + 1):
            if target_column == column:
                continue
            source = cells_by_position.get((row, target_column))
            if not source or not source.get("text"):
                continue
            if source.get("is_sku"):
                continue
            role = role_by_column.get(target_column, {})
            if role.get("role") not in {"attribute_column", "inactive"}:
                continue
            leaf_target = leaves[target_column - 1]
            source_attribute = leaf_target.source_path
            normalized = leaf_target.normalized_path
            if source_attribute.startswith("column_") or normalized.startswith("column_"):
                normalized = f"unlabelled_column_{target_column}"
                source_attribute = f"unlabelled_column_{target_column}"
                base_confidence = 0.78
            else:
                base_confidence = 0.99
            sku_crossed = sum(
                role_by_column.get(c, {}).get("role") in {"sku_column", "sku_header_column"}
                for c in range(min(column, target_column) + 1, max(column, target_column))
            )
            relationship = "same_row_left" if target_column < column else "same_row_right"
            if sku_crossed:
                relationship += "_across_sku_columns"
            inherited = bool(source.get("inherited_from_merged_cell"))
            confidence = base_confidence - (0.01 if sku_crossed else 0.0) - (0.01 if inherited else 0.0)
            decision = "accepted" if confidence >= 0.90 else "flagged"
            candidates.append(_candidate_row(
                anchor=anchor, source_cell=source, source_attribute=source_attribute,
                normalized_attribute=normalized, value=source["text"], relationship=relationship,
                confidence=confidence, decision=decision, header_path=leaf_target.components,
                cells_crossed=abs(target_column - column) - 1, sku_cells_crossed=sku_crossed,
                inherited=inherited,
            ))

        # Footnote relationships are attached only to the SKU cell containing
        # the marker.
        sku_cell = cells_by_position.get((row, column), {})
        markers = re.findall(r"\(\*+\)", sku_cell.get("text", ""))
        for marker in dict.fromkeys(markers):
            candidates.append(_candidate_row(
                anchor=anchor, source_cell=sku_cell, source_attribute="footnote_marker",
                normalized_attribute="footnote_marker", value=marker,
                relationship="sku_cell_marker", confidence=0.99, decision="accepted",
            ))
            note = footnotes.get(marker, "")
            if note:
                candidates.append(_candidate_row(
                    anchor=anchor, source_cell=sku_cell, source_attribute="footnote_text",
                    normalized_attribute="footnote_text", value=note,
                    relationship="footnote_lookup", confidence=0.96, decision="accepted",
                ))
                for rule in profile.get("footnote_rules", []):
                    try:
                        if re.search(str(rule.get("pattern", "")), note):
                            candidates.append(_candidate_row(
                                anchor=anchor, source_cell=sku_cell,
                                source_attribute=str(rule.get("attribute", "footnote_attribute")),
                                normalized_attribute=str(rule.get("attribute", "footnote_attribute")),
                                value=str(rule.get("value", "Yes")),
                                relationship="footnote_semantic_rule", confidence=0.94,
                                decision="accepted", inherited=True,
                            ))
                    except re.error:
                        continue
    return candidates


def accepted_relationships(candidates: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    return [dict(candidate) for candidate in candidates if candidate.get("decision") in {"accepted", "flagged"}]


def make_product_records(
    anchors: Sequence[dict[str, Any]], relationships: Sequence[dict[str, Any]],
    *, page: int, table: int, classification: dict[str, Any],
) -> list[dict[str, Any]]:
    by_anchor: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for relation in relationships:
        by_anchor[str(relation["anchor_id"])].append(relation)
    records: list[dict[str, Any]] = []
    for anchor in anchors:
        confidence = min([float(item.get("confidence", 0.0)) for item in by_anchor.get(anchor["anchor_id"], [])] or [float(anchor["confidence"])])
        status = "accepted" if confidence >= 0.90 else "flagged" if confidence >= 0.70 else "unresolved"
        observations: list[dict[str, Any]] = []
        attributes: dict[str, str] = {}
        for relation in by_anchor.get(anchor["anchor_id"], []):
            observation = {
                "source_attribute": relation["source_attribute"],
                "normalized_attribute": relation["normalized_attribute"],
                "value": relation["candidate_value"],
                "value_raw": relation["candidate_value"],
                "value_normalized": relation["value_normalized"],
                "unit": relation["unit"],
                "datatype": parse_value(relation["candidate_value"], DEFAULT_PROFILE)["datatype"],
                "source_row": relation["source_row"],
                "source_column": relation["source_column"],
                "source_cell_id": relation["attribute_cell_id"],
                "source_bbox": relation["source_bbox"],
                "header_path": relation["header_path"],
                "relationship_type": relation["relationship_direction"],
                "inherited": relation["inherited"],
                "confidence": relation["confidence"],
                "decision": relation["decision"],
            }
            observations.append(observation)
            name = str(relation["normalized_attribute"])
            value = str(relation["candidate_value"])
            if name and value:
                existing = attributes.get(name)
                if existing is None:
                    attributes[name] = value
                elif value not in existing.split(" | "):
                    attributes[name] = existing + " | " + value
        records.append({
            "product_code": anchor["sku"],
            "product_code_raw": anchor["sku"],
            "product_code_normalized": anchor["sku_normalized"],
            "code_position": anchor["orientation"],
            "attributes": attributes,
            "attribute_observations": observations,
            "source_page": page,
            "source_table": table,
            "source_rows": [anchor["row"]],
            "source_columns": [anchor["column"]],
            "method": f"sku_anchor_relationship_{classification.get('selected', 'unresolved')}",
            "registry_match": anchor["registry_match_type"],
            "registry_status": anchor["registry_status"],
            "registry_canonical": anchor["registry_match_type"] == "exact_normalized",
            "normalization_confidence": round(confidence, 4),
            "normalization_status": status,
            "anchor_id": anchor["anchor_id"],
        })
    return merge_duplicate_records(records)


def merge_duplicate_records(records: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[tuple[str, int, int, int, int], dict[str, Any]] = {}
    for record in records:
        rows = record.get("source_rows", []) or [0]
        columns = record.get("source_columns", []) or [0]
        key = (
            normalize_code(record.get("product_code", "")), int(record.get("source_page", 0)),
            int(record.get("source_table", 0)), int(rows[0]), int(columns[0]),
        )
        if key not in merged:
            merged[key] = json.loads(json.dumps(record))
            continue
        current = merged[key]
        for observation in record.get("attribute_observations", []):
            if observation not in current["attribute_observations"]:
                current["attribute_observations"].append(observation)
        for name, value in record.get("attributes", {}).items():
            if name not in current["attributes"]:
                current["attributes"][name] = value
            elif value not in current["attributes"][name].split(" | "):
                current["attributes"][name] += " | " + value
    return sorted(merged.values(), key=lambda item: (item["source_page"], item["source_table"], item["source_rows"], item["source_columns"], item["product_code"]))


def classify_table(
    rows: Sequence[Sequence[str]], header_rows: int, leaves: Sequence[HeaderLeaf],
    column_roles: Sequence[dict[str, Any]], anchors: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    data_anchors = [anchor for anchor in anchors if anchor["orientation"] != "column_header_index"]
    header_anchors = [anchor for anchor in anchors if anchor["orientation"] == "column_header_index"]
    sku_columns = sorted({int(anchor["column"]) for anchor in data_anchors})
    anchors_by_row: dict[int, int] = defaultdict(int)
    for anchor in data_anchors:
        anchors_by_row[int(anchor["row"])] += 1
    average = statistics.mean(anchors_by_row.values()) if anchors_by_row else 0.0
    attribute_columns = [role["column"] for role in column_roles if role["role"] == "attribute_column"]
    application_columns = [role["column"] for role in column_roles if role["normalized_header"] == "applications"]
    scores = {
        "row_records": 0.0,
        "multi_sku_row_matrix": 0.0,
        "sku_column_matrix": 0.0,
        "continuation_fragment": 0.0,
        "key_value_dimensions": 0.0,
        "compatibility_matrix": 0.0,
    }
    reasons: dict[str, list[str]] = defaultdict(list)
    if len(sku_columns) == 1:
        scores["row_records"] += 0.65
        reasons["row_records"].append("One SKU-bearing data column.")
    if anchors_by_row and sum(value > 0 for value in anchors_by_row.values()) >= 2:
        scores["row_records"] += 0.15
    if len(sku_columns) >= 2:
        scores["multi_sku_row_matrix"] += 0.55
        reasons["multi_sku_row_matrix"].append(f"{len(sku_columns)} SKU-bearing data columns.")
    if average >= 2:
        scores["multi_sku_row_matrix"] += 0.25
    if attribute_columns:
        scores["multi_sku_row_matrix"] += 0.15
    if len(header_anchors) >= 2:
        scores["sku_column_matrix"] += 0.75
        scores["compatibility_matrix"] += 0.55
        reasons["sku_column_matrix"].append("Several registry SKU anchors occur in the header band.")
    if len(sku_columns) >= 4 and len(attribute_columns) <= 2:
        scores["continuation_fragment"] += 0.65
        reasons["continuation_fragment"].append("SKU-dense matrix with few local attributes.")
    if application_columns:
        scores["continuation_fragment"] += 0.20
    dimension_names = {"length", "width", "height_depth", "diameter"}
    if len(sku_columns) == 1 and any(role["normalized_header"] in dimension_names for role in column_roles):
        scores["key_value_dimensions"] += 0.80
    if len(header_anchors) >= 2 and attribute_columns:
        scores["compatibility_matrix"] += 0.25
    ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
    selected, raw_score = ranked[0]
    second = ranked[1][1] if len(ranked) > 1 else 0.0
    margin = raw_score - second
    confidence = raw_score if margin >= 0.12 else max(0.0, raw_score - (0.12 - margin))
    if raw_score < 0.55:
        selected = "unresolved"
        confidence = min(confidence, 0.69)
    return {
        "selected": selected,
        "confidence": round(min(1.0, confidence), 4),
        "scores": [{"name": name, "score": round(score, 4), "reasons": reasons[name]} for name, score in ranked],
        "sku_columns": sku_columns,
        "attribute_columns": attribute_columns,
        "application_columns": application_columns,
        "rows_with_sku": len(anchors_by_row),
        "data_rows": max(0, len(rows) - header_rows),
        "average_skus_per_product_row": round(float(average), 4),
        "reasons": reasons[selected],
    }


def row_models_from_relationships(
    rows: Sequence[Sequence[str]], header_rows: int, anchors: Sequence[dict[str, Any]], relationships: Sequence[dict[str, Any]]
) -> list[dict[str, Any]]:
    anchors_by_row: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for anchor in anchors:
        if anchor["orientation"] != "column_header_index":
            anchors_by_row[int(anchor["row"])].append(anchor)
    rel_by_row: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for relation in relationships:
        anchor = next((item for item in anchors if item["anchor_id"] == relation["anchor_id"]), None)
        if anchor:
            rel_by_row[int(anchor["row"])].append(relation)
    models: list[dict[str, Any]] = []
    for row_number in range(header_rows + 1, len(rows) + 1):
        row_anchors = anchors_by_row.get(row_number, [])
        if not row_anchors:
            continue
        # De-duplicate shared attributes across anchors in the same row.
        shared: list[dict[str, Any]] = []
        seen: set[tuple[str, str, int | str]] = set()
        for relation in rel_by_row.get(row_number, []):
            name = relation["normalized_attribute"]
            if name in {"source_column_path", "product_type", "ip_rating", "wiring_type", "footnote_marker", "footnote_text", "phase_inverter"}:
                continue
            key = (name, relation["candidate_value"], relation.get("source_column", ""))
            if key in seen:
                continue
            seen.add(key)
            shared.append({
                "column": relation.get("source_column"),
                "source_attribute": relation["source_attribute"],
                "normalized_attribute": name,
                "value": relation["candidate_value"],
                "source_row": relation.get("source_row"),
                "confidence": relation.get("confidence"),
            })
        shared_key = tuple(sorted((item["normalized_attribute"], item["value"]) for item in shared if item["normalized_attribute"] != "applications"))
        applications = [item["value"] for item in shared if item["normalized_attribute"] == "applications"]
        models.append({
            "row_number": row_number,
            "codes": [{"code": anchor["sku"], "column": anchor["column"], "anchor_id": anchor["anchor_id"]} for anchor in row_anchors],
            "shared_attributes": shared,
            "shared_key": [list(item) for item in shared_key],
            "applications": " | ".join(dict.fromkeys(applications)),
        })
    return models


def extract_footnotes(page: pymupdf.Page, bbox: Sequence[float]) -> dict[str, str]:
    x0, _, x1, y1 = map(float, bbox)
    clip = pymupdf.Rect(max(0, x0 - 5), y1, min(page.rect.width, x1 + 5), min(page.rect.height, y1 + 110))
    text = page.get_text("text", clip=clip, sort=True)
    result: dict[str, str] = {}
    for line in text.splitlines():
        line = one_line(line)
        match = re.match(r"^(\(\*+\)|\*+)\s*(.+)$", line)
        if match:
            marker = match.group(1)
            if not marker.startswith("("):
                marker = f"({marker})"
            result[marker] = one_line(match.group(2))
    return result


# ---------------------------------------------------------------------------
# Main table analysis
# ---------------------------------------------------------------------------


def analyse_table(
    *, page: pymupdf.Page, page_number: int, table_number: int, bbox: Sequence[float],
    rows: Sequence[Sequence[str]], cells: Sequence[Sequence[Sequence[float] | None]],
    registry: dict[str, dict[str, Any]], profile: dict[str, Any] | None = None,
) -> TableAnalysis:
    profile = deep_merge_profile(DEFAULT_PROFILE, profile)
    patterns = compile_sku_patterns(profile)
    option_codes = {normalize_code(str(value)) for value in profile.get("option_codes", [])}
    selected, extraction_selection = select_matrix_candidate(page, bbox, rows, cells, registry, profile)
    materialized = [list(map(clean, row)) for row in selected.rows]
    materialized_cells = selected.cells
    materialized_nulls = selected.nulls
    header_rows = find_header_row_count(materialized, registry, patterns, option_codes)
    leaves = header_hierarchy(page, materialized, materialized_cells, selected.bbox, header_rows, profile)
    materialized, materialized_cells, materialized_nulls, leaves, dropped_columns = drop_inactive_columns(
        materialized, materialized_cells, materialized_nulls, leaves, header_rows,
        registry, patterns, option_codes,
    )
    column_roles = classify_columns(materialized, header_rows, leaves, registry, patterns, option_codes, profile)
    merged_events = []
    for event in selected.diagnostics.get("merged_expansions", []):
        old_col = int(event["target_column"])
        if old_col in dropped_columns:
            continue
        shift = sum(column < old_col for column in dropped_columns)
        converted = dict(event)
        converted["target_column"] = old_col - shift
        source_col = int(event["source_column"])
        if source_col not in dropped_columns:
            converted["source_column"] = source_col - sum(column < source_col for column in dropped_columns)
        merged_events.append(converted)
    cell_graph = build_cell_graph(
        materialized, materialized_cells, materialized_nulls, header_rows, leaves, column_roles,
        registry, patterns, option_codes, profile, merged_events,
    )
    cell_graph["extraction_selection"] = extraction_selection
    cell_graph["dropped_inactive_columns"] = dropped_columns
    anchors = make_sku_anchors(cell_graph, registry)
    footnotes = extract_footnotes(page, selected.bbox)
    candidates = build_attribute_candidates(materialized, leaves, cell_graph, anchors, profile, footnotes)
    relationships = accepted_relationships(candidates)
    classification = classify_table(materialized, header_rows, leaves, column_roles, anchors)
    records = make_product_records(anchors, relationships, page=page_number, table=table_number, classification=classification)
    row_models = row_models_from_relationships(materialized, header_rows, anchors, relationships)

    auto_threshold = float(profile.get("auto_accept_confidence", 0.90))
    flag_threshold = float(profile.get("flag_confidence", 0.70))
    accepted_count = sum(float(item["confidence"]) >= auto_threshold for item in relationships)
    flagged_count = sum(flag_threshold <= float(item["confidence"]) < auto_threshold for item in relationships)
    unresolved_count = sum(item["decision"] == "rejected" for item in candidates)
    record_status_counts: dict[str, int] = defaultdict(int)
    for record in records:
        record_status_counts[record["normalization_status"]] += 1
    validation_issues: list[str] = []
    if extraction_selection["selected"] == "provided_layout" and extraction_selection["candidates"] and len(extraction_selection["candidates"]) > 1:
        validation_issues.append("The provided layout remained the strongest SKU-preserving candidate.")
    if classification["confidence"] < flag_threshold:
        validation_issues.append("Table orientation is unresolved; attributes below the confidence threshold are not automatically accepted.")
    if any(anchor["registry_match_type"] == "unconfirmed_pattern" for anchor in anchors):
        validation_issues.append("One or more unregistered SKU anchors require review.")
    if flagged_count:
        validation_issues.append(f"{flagged_count} attribute relationships are flagged rather than auto-accepted.")
    validation = {
        "normalization_status": "accepted" if records and not validation_issues and all(record["normalization_status"] == "accepted" for record in records) else "needs_review" if records else "unresolved",
        "selected_matrix_source": extraction_selection["selected"],
        "table_orientation": classification["selected"],
        "orientation_confidence": classification["confidence"],
        "sku_anchor_count": len(anchors),
        "registered_anchor_count": sum(anchor["registry_match_type"] == "exact_normalized" for anchor in anchors),
        "unconfirmed_anchor_count": sum(anchor["registry_match_type"] == "unconfirmed_pattern" for anchor in anchors),
        "attribute_candidate_count": len(candidates),
        "accepted_relationship_count": accepted_count,
        "flagged_relationship_count": flagged_count,
        "rejected_relationship_count": unresolved_count,
        "product_record_count": len(records),
        "product_record_status_counts": dict(record_status_counts),
        "issues": validation_issues,
        "precision_policy": {
            "auto_accept": auto_threshold,
            "flag": flag_threshold,
            "below_flag": "SKU occurrence retained; uncertain attribute not accepted.",
        },
    }
    return TableAnalysis(
        page=page_number,
        table=table_number,
        bbox=selected.bbox,
        header_rows=header_rows,
        leaf_headers=leaves,
        reconstructed_columns=[leaf.source_path for leaf in leaves],
        reconstructed_rows=[[one_line(value) for value in row] for row in materialized[header_rows:]],
        classification=classification,
        product_records=records,
        validation=validation,
        footnotes=footnotes,
        row_models=row_models,
        cell_graph=cell_graph,
        sku_anchors=anchors,
        header_paths=header_paths_rows(leaves, column_roles),
        attribute_candidates=candidates,
        attribute_relationships=relationships,
        extraction_selection=extraction_selection,
    )


# ---------------------------------------------------------------------------
# Artifacts
# ---------------------------------------------------------------------------


def product_records_to_wide(records: Sequence[dict[str, Any]]) -> tuple[list[str], list[list[str]]]:
    normalized_names = sorted({
        str(obs.get("normalized_attribute", ""))
        for record in records for obs in record.get("attribute_observations", [])
        if obs.get("normalized_attribute")
    })
    columns = [
        "product_code", "normalization_status", "normalization_confidence",
        "source_page", "source_table", "source_rows", "source_columns", *normalized_names,
    ]
    output: list[list[str]] = []
    for record in records:
        attrs: dict[str, list[str]] = defaultdict(list)
        for obs in record.get("attribute_observations", []):
            name = str(obs.get("normalized_attribute", ""))
            value = str(obs.get("value", ""))
            if name and value and value not in attrs[name]:
                attrs[name].append(value)
        output.append([
            str(record.get("product_code", "")), str(record.get("normalization_status", "")),
            str(record.get("normalization_confidence", "")), str(record.get("source_page", "")),
            str(record.get("source_table", "")), ";".join(map(str, record.get("source_rows", []))),
            ";".join(map(str, record.get("source_columns", []))),
            *[" | ".join(attrs.get(name, [])) for name in normalized_names],
        ])
    return columns, output


def write_analysis_artifacts(directory: Path, analysis: TableAnalysis) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    # New diagnostic contract.
    write_json(directory / "04_cell_graph.json", analysis.cell_graph)
    write_csv(directory / "05_reconstructed_table.csv", [analysis.reconstructed_columns, *analysis.reconstructed_rows])
    write_dict_csv(directory / "06_sku_anchors.csv", analysis.sku_anchors)
    write_dict_csv(directory / "07_header_paths.csv", analysis.header_paths)
    write_dict_csv(directory / "08_attribute_candidates.csv", analysis.attribute_candidates)
    write_dict_csv(directory / "09_attribute_relationships.csv", analysis.attribute_relationships)
    product_columns, product_rows = product_records_to_wide(analysis.product_records)
    write_csv(directory / "10_normalized_product_records.csv", [product_columns, *product_rows])
    write_json(directory / "11_table_classification.json", analysis.classification)
    write_json(directory / "12_validation_report.json", analysis.validation)
    write_json(directory / "matrix_selection.json", analysis.extraction_selection)

    # Compatibility names used by earlier main-script aggregation logic.
    write_json(directory / "01_detected_cells.json", analysis.cell_graph)
    write_csv(directory / "02_reconstructed_table.csv", [analysis.reconstructed_columns, *analysis.reconstructed_rows])
    write_json(directory / "03_header_hierarchy.json", [asdict(item) for item in analysis.leaf_headers])
    write_json(directory / "04_table_classification.json", analysis.classification)
    write_csv(directory / "05_normalized_product_records.csv", [product_columns, *product_rows])
    write_json(directory / "06_validation_report.json", analysis.validation)
    write_json(directory / "table_model.json", {
        "page": analysis.page,
        "table": analysis.table,
        "bbox": analysis.bbox,
        "header_rows": analysis.header_rows,
        "leaf_headers": [asdict(item) for item in analysis.leaf_headers],
        "reconstructed_columns": analysis.reconstructed_columns,
        "reconstructed_rows": analysis.reconstructed_rows,
        "classification": analysis.classification,
        "product_records": analysis.product_records,
        "validation": analysis.validation,
        "footnotes": analysis.footnotes,
        "row_models": analysis.row_models,
        "sku_anchors": analysis.sku_anchors,
        "attribute_relationships": analysis.attribute_relationships,
        "extraction_selection": analysis.extraction_selection,
    })


# ---------------------------------------------------------------------------
# Continuation joins
# ---------------------------------------------------------------------------


def _collapsed_primary_runs(model: dict[str, Any]) -> list[dict[str, Any]]:
    runs: list[dict[str, Any]] = []
    for row in model.get("row_models", []):
        key = tuple(tuple(item) for item in row.get("shared_key", []))
        if not row.get("codes"):
            continue
        if runs and runs[-1]["key"] == key:
            runs[-1]["row_numbers"].append(row["row_number"])
            runs[-1]["codes"].extend(row.get("codes", []))
        else:
            runs.append({
                "key": key,
                "row_numbers": [row["row_number"]],
                "codes": list(row.get("codes", [])),
                "shared_attributes": list(row.get("shared_attributes", [])),
            })
    return runs


def _header_signature(model: dict[str, Any]) -> set[str]:
    values: set[str] = set()
    for leaf in model.get("leaf_headers", []):
        for component in leaf.get("components", []):
            component = one_line(component)
            if component and not re.search(r"(?i)^applications?$", component):
                values.add(slug(component))
    return values


def continuation_score(primary: dict[str, Any], secondary: dict[str, Any]) -> dict[str, Any]:
    score = 0.0
    reasons: list[str] = []
    if int(secondary.get("page", 0)) == int(primary.get("page", 0)) + 1:
        score += 0.15
        reasons.append("Consecutive PDF pages.")
    primary_orientation = primary.get("classification", {}).get("selected")
    secondary_orientation = secondary.get("classification", {}).get("selected")
    if primary_orientation == "multi_sku_row_matrix":
        score += 0.15
        reasons.append("Primary table contains shared attributes and multiple SKU columns.")
    if secondary_orientation in {"continuation_fragment", "multi_sku_row_matrix"}:
        score += 0.15
        reasons.append("Secondary table is a compatible SKU matrix.")
    runs = _collapsed_primary_runs(primary)
    secondary_rows = [row for row in secondary.get("row_models", []) if row.get("codes")]
    exact = len(runs) == len(secondary_rows) and bool(runs)
    ratio = min(len(runs), len(secondary_rows)) / max(1, max(len(runs), len(secondary_rows))) if runs and secondary_rows else 0.0
    if exact:
        score += 0.40
        reasons.append("Compressed primary row groups exactly match secondary SKU rows.")
    else:
        score += 0.20 * ratio
    primary_sig = _header_signature(primary)
    secondary_sig = _header_signature(secondary)
    overlap = len(primary_sig & secondary_sig) / max(1, min(len(primary_sig), len(secondary_sig)))
    if overlap >= 0.3:
        score += 0.10
        reasons.append(f"Header-family overlap {overlap:.2f}.")
    secondary_applications = any(row.get("applications") for row in secondary_rows)
    if secondary_applications:
        score += 0.05
        reasons.append("Secondary table contributes right-side applications.")
    # Contradiction guard: a continuation must be complementary, not a second
    # complete table with conflicting shared specifications.
    secondary_shared = sum(bool(row.get("shared_key")) for row in secondary_rows)
    contradiction = secondary_shared > max(2, len(secondary_rows) * 0.5)
    if contradiction:
        score -= 0.25
        reasons.append("Secondary table contains many independent shared specifications.")
    return {
        "score": round(max(0.0, min(1.0, score)), 4),
        "reasons": reasons,
        "primary_run_count": len(runs),
        "secondary_row_count": len(secondary_rows),
        "row_count_exact": exact,
        "row_count_similarity": round(ratio, 4),
        "header_overlap": round(overlap, 4),
        "contradiction": contradiction,
    }


def _append_join_observation(record: dict[str, Any], attribute: dict[str, Any], *, from_page: int, from_table: int, confidence: float) -> None:
    value = one_line(attribute.get("value", ""))
    if not value:
        return
    parsed = parse_value(value, DEFAULT_PROFILE)
    observation = {
        "source_attribute": attribute.get("source_attribute") or attribute.get("normalized_attribute") or "attribute",
        "normalized_attribute": attribute.get("normalized_attribute") or "attribute",
        "value": value,
        **parsed,
        "source_row": attribute.get("source_row"),
        "source_column": attribute.get("source_column"),
        "source_cell_id": attribute.get("source_cell_id", ""),
        "source_bbox": attribute.get("source_bbox"),
        "header_path": attribute.get("header_path", []),
        "relationship_type": "continuation_join",
        "inherited": True,
        "inherited_from_page": from_page,
        "inherited_from_table": from_table,
        "confidence": confidence,
        "decision": "accepted" if confidence >= 0.90 else "flagged",
    }
    if observation not in record.setdefault("attribute_observations", []):
        record["attribute_observations"].append(observation)
    name = observation["normalized_attribute"]
    existing = record.setdefault("attributes", {}).get(name)
    if existing is None:
        record["attributes"][name] = value
    elif value not in existing.split(" | "):
        record["attributes"][name] += " | " + value


def apply_continuation_join(primary: dict[str, Any], secondary: dict[str, Any], score_info: dict[str, Any], profile: dict[str, Any]) -> dict[str, Any]:
    runs = _collapsed_primary_runs(primary)
    secondary_rows = [row for row in secondary.get("row_models", []) if row.get("codes")]
    join_id = f"p{primary['page']}_t{primary['table']}__p{secondary['page']}_t{secondary['table']}"
    primary_by_row: dict[int, list[dict[str, Any]]] = defaultdict(list)
    secondary_by_row: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for record in primary.get("product_records", []):
        for row in record.get("source_rows", []):
            primary_by_row[int(row)].append(record)
    for record in secondary.get("product_records", []):
        for row in record.get("source_rows", []):
            secondary_by_row[int(row)].append(record)

    mappings: list[dict[str, Any]] = []
    for run, secondary_row in zip(runs, secondary_rows):
        application = one_line(secondary_row.get("applications", ""))
        primary_product_type = ""
        for row_number in run["row_numbers"]:
            for record in primary_by_row.get(int(row_number), []):
                for obs in record.get("attribute_observations", []):
                    if obs.get("normalized_attribute") == "product_type" and obs.get("value"):
                        primary_product_type = str(obs["value"])
                        break
                if primary_product_type:
                    break
            if primary_product_type:
                break

        if application:
            for row_number in run["row_numbers"]:
                for record in primary_by_row.get(int(row_number), []):
                    _append_join_observation(record, {
                        "source_attribute": "APPLICATIONS", "normalized_attribute": "applications",
                        "value": application, "source_row": secondary_row["row_number"], "source_column": None,
                    }, from_page=int(secondary["page"]), from_table=int(secondary["table"]), confidence=score_info["score"])

        for record in secondary_by_row.get(int(secondary_row["row_number"]), []):
            # Preserve continuation family separately, while the primary product
            # family remains authoritative as agreed.
            for obs in record.get("attribute_observations", []):
                if obs.get("normalized_attribute") == "product_type" and primary_product_type and one_line(obs.get("value")) != one_line(primary_product_type):
                    obs["source_attribute"] = "continuation_product_type"
                    obs["normalized_attribute"] = "continuation_product_type"
            if primary_product_type:
                _append_join_observation(record, {
                    "source_attribute": "product_type", "normalized_attribute": "product_type",
                    "value": primary_product_type, "source_row": run["row_numbers"][0], "source_column": None,
                }, from_page=int(primary["page"]), from_table=int(primary["table"]), confidence=score_info["score"])
            for attribute in run.get("shared_attributes", []):
                _append_join_observation(record, attribute, from_page=int(primary["page"]), from_table=int(primary["table"]), confidence=score_info["score"])
            if application:
                _append_join_observation(record, {
                    "source_attribute": "APPLICATIONS", "normalized_attribute": "applications",
                    "value": application, "source_row": secondary_row["row_number"], "source_column": None,
                }, from_page=int(secondary["page"]), from_table=int(secondary["table"]), confidence=score_info["score"])
            record["continuation_join_id"] = join_id
            record["continuation_join_confidence"] = score_info["score"]
        for row_number in run["row_numbers"]:
            for record in primary_by_row.get(int(row_number), []):
                record["continuation_join_id"] = join_id
                record["continuation_join_confidence"] = score_info["score"]
        mappings.append({
            "primary_rows": run["row_numbers"],
            "secondary_row": secondary_row["row_number"],
            "shared_key": [list(item) for item in run["key"]],
            "applications": application,
            "primary_skus": [item["code"] for item in run.get("codes", [])],
            "secondary_skus": [item["code"] for item in secondary_row.get("codes", [])],
        })
    primary["product_records"] = merge_duplicate_records(primary.get("product_records", []))
    secondary["product_records"] = merge_duplicate_records(secondary.get("product_records", []))
    return {
        "join_id": join_id,
        "status": "auto_joined",
        "confidence": score_info["score"],
        "score_details": score_info,
        "primary": {"page": primary["page"], "table": primary["table"]},
        "secondary": {"page": secondary["page"], "table": secondary["table"]},
        "row_mappings": mappings,
    }


def discover_and_apply_continuations(
    table_models: list[dict[str, Any]], profile: dict[str, Any] | None = None,
    *, auto_threshold: float = 0.90, review_threshold: float = 0.70,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    profile = deep_merge_profile(DEFAULT_PROFILE, profile)
    models = sorted(table_models, key=lambda item: (int(item.get("page", 0)), int(item.get("table", 0))))
    joins: list[dict[str, Any]] = []
    reviews: list[dict[str, Any]] = []
    used_secondary: set[tuple[int, int]] = set()
    for primary in models:
        for secondary in models:
            if int(secondary.get("page", 0)) != int(primary.get("page", 0)) + 1:
                continue
            key = (int(secondary.get("page", 0)), int(secondary.get("table", 0)))
            if key in used_secondary:
                continue
            info = continuation_score(primary, secondary)
            if info["score"] >= auto_threshold and info["row_count_exact"] and not info["contradiction"]:
                joins.append(apply_continuation_join(primary, secondary, info, profile))
                used_secondary.add(key)
                break
            if info["score"] >= review_threshold:
                reviews.append({
                    "status": "review_required",
                    "primary": {"page": primary.get("page"), "table": primary.get("table")},
                    "secondary": {"page": secondary.get("page"), "table": secondary.get("table")},
                    **info,
                })
    return joins, reviews


# ---------------------------------------------------------------------------
# Standalone regression utility
# ---------------------------------------------------------------------------


def analyse_pdf_with_line_grids(
    pdf_path: Path | str, output: Path | str,
    *, registry: dict[str, dict[str, Any]] | None = None, profile: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    registry = registry or {}
    profile = deep_merge_profile(DEFAULT_PROFILE, profile)
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    doc = pymupdf.open(pdf_path)
    models: list[dict[str, Any]] = []
    try:
        for page_index, page in enumerate(doc):
            finder = page.find_tables(strategy="lines")
            if not finder.tables:
                continue
            table = max(finder.tables, key=lambda item: item.row_count * item.col_count)
            rows = table.extract()
            cells = [[normalize_bbox(cell) if cell else None for cell in row.cells] for row in table.rows]
            analysis = analyse_table(
                page=page, page_number=page_index + 1, table_number=1,
                bbox=table.bbox, rows=rows, cells=cells, registry=registry, profile=profile,
            )
            directory = output / f"page_{page_index + 1:04d}" / "table_01"
            write_analysis_artifacts(directory, analysis)
            model = json.loads((directory / "table_model.json").read_text(encoding="utf-8"))
            models.append(model)
        joins, reviews = discover_and_apply_continuations(models, profile)
        write_json(output / "continuation_joins.json", joins)
        write_json(output / "continuation_join_review.json", reviews)
        for model in models:
            directory = output / f"page_{int(model['page']):04d}" / f"table_{int(model['table']):02d}"
            write_json(directory / "table_model.json", model)
            columns, rows = product_records_to_wide(model.get("product_records", []))
            write_csv(directory / "05_normalized_product_records.csv", [columns, *rows])
            write_csv(directory / "10_normalized_product_records.csv", [columns, *rows])
            related = [
                join for join in joins
                if join.get("primary") == {"page": model["page"], "table": model["table"]}
                or join.get("secondary") == {"page": model["page"], "table": model["table"]}
            ]
            if related:
                write_json(directory / "13_continuation_evidence.json", related)
        return models
    finally:
        doc.close()
