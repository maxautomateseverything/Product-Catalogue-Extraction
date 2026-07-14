#!/usr/bin/env python3
"""
Catalogue Contents Extractor
----------------------------
Config-driven extraction of catalogue contents pages from selectable-text PDFs.

Main supported modes:
  - style_rules: classify visual lines by style, position, regex, and colour.
  - region_rules: same as style_rules but constrained by user-defined regions; also supports area_table_groups.
  - card_grid: pair page-number regions with title regions for visual contents card layouts.

Usage:
  py catalogue_contents_extractor.py --config contents_config.yaml
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import re
import shutil
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from statistics import median
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

try:
    import fitz
except ImportError as exc:  # pragma: no cover
    raise SystemExit("Missing dependency: PyMuPDF. Install with: py -m pip install PyMuPDF") from exc

try:
    import pandas as pd
except ImportError as exc:  # pragma: no cover
    raise SystemExit("Missing dependency: pandas. Install with: py -m pip install pandas openpyxl") from exc

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:  # pragma: no cover
    Image = None
    ImageDraw = None
    ImageFont = None


DEFAULT_PAGE_REGEX = r"(?i)(?:see\s+page\s+)?[A-Z]?\d+(?:\s*(?:,|/|;|-|–|—)\s*[A-Z]?\d+)*"
OUTPUT_ALIASES = {
    "Main header": "main_header",
    "Subheader 1": "subheader_1",
    "Subheader 2": "subheader_2",
    "Subheader 3": "subheader_3",
    "Page Number": "page_number",
    "Subheader 2 + Page Number Combined": "subheader_2_page_number_combined",
}


def log(message: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


def load_config(path: Path) -> Dict[str, Any]:
    if not path.exists():
        raise SystemExit(f"Config not found: {path}")
    with path.open("r", encoding="utf-8") as f:
        if path.suffix.lower() in {".yaml", ".yml"}:
            if yaml is None:
                raise SystemExit("PyYAML is required to read YAML config files.")
            return yaml.safe_load(f) or {}
        return json.load(f)


def save_config(config: Dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        if path.suffix.lower() in {".yaml", ".yml"} and yaml is not None:
            yaml.safe_dump(config, f, sort_keys=False, allow_unicode=True)
        else:
            json.dump(config, f, indent=2, ensure_ascii=False)


def parse_page_range(page_range: Any, total_pages: Optional[int] = None) -> List[int]:
    raw = str(page_range or "").strip().lower()
    if not raw:
        return []
    if raw == "all":
        if total_pages is None:
            raise ValueError("'all' page range requires total_pages")
        return list(range(1, total_pages + 1))
    pages: List[int] = []
    for part in raw.split(","):
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
    out: List[int] = []
    for p in pages:
        if total_pages is not None and (p < 1 or p > total_pages):
            continue
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out


def page_applies(page: int, page_range: Any, total_pages: Optional[int] = None) -> bool:
    if page_range in (None, "", "all"):
        return True
    return page in set(parse_page_range(page_range, total_pages))


def collapse_ws(text: Any) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def srgb_to_hex(value: Any) -> str:
    try:
        return f"#{int(value or 0) & 0xFFFFFF:06X}"
    except Exception:
        return ""


def hex_equal(a: Any, b: Any) -> bool:
    if not a or not b:
        return False
    return str(a).strip().upper() == str(b).strip().upper()


def rect_from_cfg(cfg: Dict[str, Any]) -> Tuple[float, float, float, float]:
    return float(cfg["x0"]), float(cfg["y0"]), float(cfg["x1"]), float(cfg["y1"])


def rect_contains_point(rect: Tuple[float, float, float, float], x: float, y: float) -> bool:
    x0, y0, x1, y1 = rect
    return x0 <= x <= x1 and y0 <= y <= y1


def rect_intersects(a: Tuple[float, float, float, float], b: Tuple[float, float, float, float]) -> bool:
    ax0, ay0, ax1, ay1 = a
    bx0, by0, bx1, by1 = b
    return not (ax1 < bx0 or bx1 < ax0 or ay1 < by0 or by1 < ay0)


def bbox_union(records: Sequence[Dict[str, Any]]) -> Tuple[float, float, float, float]:
    return (
        min(float(r["x0"]) for r in records),
        min(float(r["y0"]) for r in records),
        max(float(r["x1"]) for r in records),
        max(float(r["y1"]) for r in records),
    )


def normalize_page_value(value: Any) -> str:
    raw = collapse_ws(value)
    if not raw:
        return ""
    cleaned = re.sub(r"(?i)\bsee\s+page\b", "", raw)
    cleaned = re.sub(r"(?i)\bpage\b", "", cleaned).strip()
    cleaned = cleaned.replace("–", "-").replace("—", "-")
    return re.sub(r"\s+", " ", cleaned)


def numeric_start_page(value: Any) -> Optional[int]:
    m = re.search(r"\d+", str(value or ""))
    return int(m.group(0)) if m else None


def format_derived_range(start_value: Any, next_start_value: Any) -> str:
    start = numeric_start_page(start_value)
    next_start = numeric_start_page(next_start_value)
    if start is None:
        return normalize_page_value(start_value)
    if next_start is None or next_start <= start:
        return str(start)
    end = next_start - 1
    return str(start) if end == start else f"{start} - {end}"


def dedupe_preserve(values: Iterable[str]) -> List[str]:
    seen = set()
    out: List[str] = []
    for v in values:
        v = collapse_ws(v)
        if v and v not in seen:
            seen.add(v)
            out.append(v)
    return out


@dataclass
class LineRecord:
    page: int
    line_id: int
    text: str
    x0: float
    y0: float
    x1: float
    y1: float
    y_mid: float
    dominant_font: str = ""
    font_names: str = ""
    median_font_size: float = 0.0
    min_font_size: float = 0.0
    max_font_size: float = 0.0
    dominant_color: str = ""
    colors: str = ""
    direction_x: Any = ""
    direction_y: Any = ""
    styles: str = ""
    region_name: str = ""
    reading_order: int = 9999
    fixed_context: Dict[str, str] = field(default_factory=dict)
    source_line_ids: List[int] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "page": self.page,
            "line_id": self.line_id,
            "text": self.text,
            "x0": round(self.x0, 3),
            "y0": round(self.y0, 3),
            "x1": round(self.x1, 3),
            "y1": round(self.y1, 3),
            "y_mid": round(self.y_mid, 3),
            "dominant_font": self.dominant_font,
            "font_names": self.font_names,
            "median_font_size": round(self.median_font_size, 3),
            "min_font_size": round(self.min_font_size, 3),
            "max_font_size": round(self.max_font_size, 3),
            "dominant_color": self.dominant_color,
            "colors": self.colors,
            "region_name": self.region_name,
            "reading_order": self.reading_order,
            "source_line_ids": ";".join(str(x) for x in self.source_line_ids or [self.line_id]),
        }


def extract_lines_for_pages(doc: fitz.Document, pages: Sequence[int], config: Dict[str, Any]) -> List[LineRecord]:
    records: List[LineRecord] = []
    line_global_id = 0
    for pno in pages:
        page = doc[pno - 1]
        raw = page.get_text("dict") or {}
        for block in raw.get("blocks", []) or []:
            if block.get("type") != 0:
                continue
            for line in block.get("lines", []) or []:
                spans = [s for s in line.get("spans", []) or [] if collapse_ws(s.get("text", ""))]
                if not spans:
                    continue
                text = collapse_ws(" ".join(str(s.get("text", "")) for s in spans))
                if not text:
                    continue
                bboxes = [s.get("bbox") for s in spans if s.get("bbox")]
                if not bboxes:
                    continue
                sizes = [float(s.get("size", 0)) for s in spans if s.get("size") is not None]
                fonts = [str(s.get("font", "")) for s in spans if s.get("font")]
                colours = [srgb_to_hex(s.get("color")) for s in spans]
                x0, y0, x1, y1 = (
                    min(float(b[0]) for b in bboxes),
                    min(float(b[1]) for b in bboxes),
                    max(float(b[2]) for b in bboxes),
                    max(float(b[3]) for b in bboxes),
                )
                direction = line.get("dir", ("", ""))
                line_global_id += 1
                records.append(LineRecord(
                    page=pno,
                    line_id=line_global_id,
                    text=text,
                    x0=x0,
                    y0=y0,
                    x1=x1,
                    y1=y1,
                    y_mid=(y0 + y1) / 2,
                    dominant_font=Counter(fonts).most_common(1)[0][0] if fonts else "",
                    font_names="; ".join(sorted(set(fonts))),
                    median_font_size=float(median(sizes)) if sizes else 0.0,
                    min_font_size=min(sizes) if sizes else 0.0,
                    max_font_size=max(sizes) if sizes else 0.0,
                    dominant_color=Counter(colours).most_common(1)[0][0] if colours else "",
                    colors="; ".join(sorted(set(colours))),
                    direction_x=direction[0] if isinstance(direction, (list, tuple)) and len(direction) > 0 else "",
                    direction_y=direction[1] if isinstance(direction, (list, tuple)) and len(direction) > 1 else "",
                    source_line_ids=[line_global_id],
                ))
    return records


def load_visual_templates_into_config(config: Dict[str, Any]) -> Dict[str, Any]:
    """Merge visual selector regions into the config if visual_templates are supplied."""
    config = json.loads(json.dumps(config))  # simple deep copy
    templates = config.get("visual_templates", []) or []
    if isinstance(templates, dict):
        templates = [templates]
    for item in templates:
        path = item.get("template_path") or item.get("path")
        if not path:
            continue
        tpath = Path(path)
        if not tpath.exists():
            log(f"Warning: visual template not found: {tpath}")
            continue
        with tpath.open("r", encoding="utf-8") as f:
            template = json.load(f)
        apply_pages = item.get("apply_pdf_pages") or template.get("apply_pdf_pages") or item.get("pages")
        for region in template.get("regions", []) or []:
            rtype = region.get("type") or region.get("region_type")
            region = dict(region)
            if apply_pages and not region.get("page_range"):
                region["page_range"] = apply_pages
            if rtype == "content_region":
                config.setdefault("content_regions", []).append(region)
            elif rtype == "ignore_region":
                config.setdefault("ignore_regions", []).append(region)
            elif rtype in {"table_column", "area_table_column"}:
                group_name = region.get("group") or region.get("group_name") or "visual_area_table"
                groups = config.setdefault("area_table_groups", [])
                group = next((g for g in groups if g.get("name") == group_name), None)
                if group is None:
                    group = {"name": group_name, "page_range": region.get("page_range", apply_pages), "fixed_context": region.get("fixed_context", {}), "columns": []}
                    groups.append(group)
                col = dict(region)
                col["field"] = region.get("field") or region.get("output_field")
                group.setdefault("columns", []).append(col)
            elif rtype in {"card_page", "card_title", "card_region", "header_region"}:
                group_name = region.get("group") or region.get("group_name") or "visual_card_group"
                groups = config.setdefault("card_grid", {}).setdefault("groups", [])
                group = next((g for g in groups if g.get("name") == group_name), None)
                if group is None:
                    group = {"name": group_name, "page_range": region.get("page_range", apply_pages), "fixed_context": region.get("fixed_context", {}), "title_regions": [], "page_number_regions": [], "card_regions": []}
                    groups.append(group)
                if rtype == "card_page":
                    group.setdefault("page_number_regions", []).append(region)
                elif rtype == "card_title":
                    group.setdefault("title_regions", []).append(region)
                elif rtype == "card_region":
                    group.setdefault("card_regions", []).append(region)
                elif rtype == "header_region":
                    group.setdefault("header_regions", []).append(region)
    return config


def apply_regions(lines: List[LineRecord], config: Dict[str, Any], total_pages: int) -> List[LineRecord]:
    content_regions = config.get("content_regions", []) or []
    ignore_regions = config.get("ignore_regions", []) or []
    if not content_regions and not ignore_regions:
        return sorted(lines, key=lambda r: (r.page, r.y0, r.x0))
    output: List[LineRecord] = []
    for line in lines:
        line_rect = (line.x0, line.y0, line.x1, line.y1)
        ignored = False
        for ir in ignore_regions:
            if not page_applies(line.page, ir.get("page_range") or ir.get("apply_pdf_pages"), total_pages):
                continue
            if rect_intersects(line_rect, rect_from_cfg(ir)):
                ignored = True
                break
        if ignored:
            continue
        if not content_regions:
            output.append(line)
            continue
        assigned = False
        cx = (line.x0 + line.x1) / 2
        cy = line.y_mid
        for region in sorted(content_regions, key=lambda r: int(r.get("reading_order", 9999))):
            if not page_applies(line.page, region.get("page_range") or region.get("apply_pdf_pages"), total_pages):
                continue
            rect = rect_from_cfg(region)
            mode = region.get("inclusion_mode", "center")
            inside = rect_contains_point(rect, cx, cy) if mode == "center" else rect_intersects(line_rect, rect)
            if inside:
                new_line = LineRecord(**line.__dict__)
                new_line.region_name = region.get("name", "")
                new_line.reading_order = int(region.get("reading_order", 9999))
                new_line.fixed_context = region.get("fixed_context", {}) or {}
                output.append(new_line)
                assigned = True
                break
        # If content regions are provided, unassigned lines are ignored.
    return sorted(output, key=lambda r: (r.page, r.reading_order, r.y0, r.x0))


def line_matches_rule(line: LineRecord, rule: Dict[str, Any], total_pages: int) -> bool:
    if not page_applies(line.page, rule.get("page_range") or rule.get("apply_pdf_pages"), total_pages):
        return False
    if rule.get("region_name") and str(rule.get("region_name")) != str(line.region_name):
        return False
    text = line.text
    if rule.get("text_regex"):
        flags = 0 if rule.get("case_sensitive", False) else re.IGNORECASE
        if not re.search(str(rule["text_regex"]), text, flags=flags):
            return False
    if rule.get("text_not_regex"):
        flags = 0 if rule.get("case_sensitive", False) else re.IGNORECASE
        if re.search(str(rule["text_not_regex"]), text, flags=flags):
            return False
    if rule.get("fontname_contains"):
        if str(rule["fontname_contains"]).lower() not in str(line.font_names or line.dominant_font).lower():
            return False
    if rule.get("font_size_min") is not None and line.median_font_size < float(rule["font_size_min"]):
        return False
    if rule.get("font_size_max") is not None and line.median_font_size > float(rule["font_size_max"]):
        return False
    if rule.get("font_height_min") is not None and (line.y1 - line.y0) < float(rule["font_height_min"]):
        return False
    if rule.get("font_height_max") is not None and (line.y1 - line.y0) > float(rule["font_height_max"]):
        return False
    if rule.get("font_width_min") is not None and (line.x1 - line.x0) < float(rule["font_width_min"]):
        return False
    if rule.get("font_width_max") is not None and (line.x1 - line.x0) > float(rule["font_width_max"]):
        return False
    if rule.get("color") and not hex_equal(rule["color"], line.dominant_color):
        return False
    for key, attr in [("x0_min", line.x0), ("x0_max", line.x0), ("x1_min", line.x1), ("x1_max", line.x1), ("top_min", line.y0), ("top_max", line.y0), ("bottom_min", line.y1), ("bottom_max", line.y1)]:
        if key in rule and rule[key] is not None:
            val = float(rule[key])
            if key.endswith("_min") and attr < val:
                return False
            if key.endswith("_max") and attr > val:
                return False
    return True


def classify_line(line: LineRecord, rules: List[Dict[str, Any]], total_pages: int) -> Tuple[str, Optional[Dict[str, Any]]]:
    for rule in sorted(rules, key=lambda r: int(r.get("priority", 1000))):
        if line_matches_rule(line, rule, total_pages):
            action = rule.get("action") or rule.get("output_type") or "ignore"
            return str(action), rule
    return "unclassified", None


def extract_page_value(text: str, regex: str, position: str = "trailing") -> Tuple[str, str]:
    """Return (item_text_without_page, page_value)."""
    pattern = re.compile(regex or DEFAULT_PAGE_REGEX)
    matches = list(pattern.finditer(text))
    if not matches:
        return collapse_ws(text), ""
    if position == "leading":
        m = matches[0]
        if m.start() > 3:
            return collapse_ws(text), ""
        page = m.group(0)
        item = text[m.end():]
    elif position == "trailing":
        m = matches[-1]
        # Usually there may be dot leaders or spaces before the page number.
        tail = text[m.end():].strip()
        if tail and not re.fullmatch(r"[.\s]*", tail):
            # If a later match is not at the end, still use it but flag by leaving full text? Keep simple.
            pass
        page = m.group(0)
        item = text[:m.start()]
        item = re.sub(r"[.\s]+$", "", item)
    else:
        m = matches[-1]
        page = m.group(0)
        item = text.replace(page, "", 1)
    return collapse_ws(item), normalize_page_value(page)


def styles_compatible(a: LineRecord, b: LineRecord, tolerance: float = 0.35) -> bool:
    return (
        a.dominant_font == b.dominant_font
        and abs(float(a.median_font_size or 0) - float(b.median_font_size or 0)) <= tolerance
        and a.dominant_color == b.dominant_color
    )


def merge_lines(a: LineRecord, b: LineRecord, separator: str = " ") -> LineRecord:
    x0, y0, x1, y1 = bbox_union([a.as_dict(), b.as_dict()])
    merged = LineRecord(**a.__dict__)
    merged.text = collapse_ws(a.text + separator + b.text)
    merged.x0, merged.y0, merged.x1, merged.y1 = x0, y0, x1, y1
    merged.y_mid = (y0 + y1) / 2
    merged.source_line_ids = (a.source_line_ids or [a.line_id]) + (b.source_line_ids or [b.line_id])
    return merged


def apply_template_map(row: Dict[str, Any], rule: Dict[str, Any], groups: Dict[str, str]) -> Dict[str, Any]:
    mapping = rule.get("map") or rule.get("output_map") or {}
    for out_field, template in mapping.items():
        result = str(template)
        for k, v in groups.items():
            result = result.replace("{" + k + "}", collapse_ws(v))
        row[out_field] = collapse_ws(result)
    return row


def build_row_from_item_line(line: LineRecord, context: Dict[str, str], rule: Optional[Dict[str, Any]], config: Dict[str, Any]) -> Dict[str, Any]:
    page_cfg = config.get("page_number", {}) or {}
    regex = (rule or {}).get("page_regex") or page_cfg.get("regex") or DEFAULT_PAGE_REGEX
    position = (rule or {}).get("page_number_position") or page_cfg.get("position") or "trailing"
    item_text, page_value = extract_page_value(line.text, regex, position)
    raw_text = line.text
    row: Dict[str, Any] = dict(context)
    row.update(line.fixed_context or {})
    row.setdefault("main_header", "")
    row.setdefault("subheader_1", "")
    row.setdefault("subheader_2", "")
    row.setdefault("subheader_3", "")
    row.setdefault("page_number", page_value)

    item_regex = (rule or {}).get("item_regex")
    if item_regex:
        m = re.match(str(item_regex), line.text)
        if m:
            groups = {k: collapse_ws(v) for k, v in m.groupdict().items() if v is not None}
            row = apply_template_map(row, rule or {}, groups)
            if "page" in groups and not row.get("page_number"):
                row["page_number"] = normalize_page_value(groups["page"])
    else:
        target_field = (rule or {}).get("item_text_field") or config.get("default_item_text_field") or "subheader_1"
        # Do not overwrite a target that was already supplied by region/fixed context unless rule says so.
        if not row.get(target_field) or (rule or {}).get("overwrite_item_text_field", True):
            row[target_field] = item_text

    if config.get("outputs", {}).get("include_combined_item_page", False):
        row["subheader_2_page_number_combined"] = raw_text

    row["source_pdf_page"] = line.page
    row["source_region"] = line.region_name
    row["source_line_ids"] = ";".join(str(x) for x in line.source_line_ids or [line.line_id])
    row["raw_text"] = raw_text
    row["classification_rule"] = (rule or {}).get("name", "default_item")
    row["confidence_status"] = "confirmed" if row.get("page_number") else "needs_review"
    row["review_reason"] = "" if row.get("page_number") else "missing_page_number"
    row["row_type"] = "content_item"
    return row


def extract_style_or_region_rows(doc: fitz.Document, pages: Sequence[int], config: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]]]:
    total_pages = doc.page_count
    raw_lines = extract_lines_for_pages(doc, pages, config)
    lines = apply_regions(raw_lines, config, total_pages)
    rules = config.get("classification_rules", []) or config.get("rules", {}).get("classification_rules", []) or []
    page_cfg = config.get("page_number", {}) or {}
    page_regex = page_cfg.get("regex") or DEFAULT_PAGE_REGEX
    page_position_default = page_cfg.get("position") or "trailing"
    multiline_cfg = config.get("multiline_items", {}) or {}
    multiline_enabled = bool(multiline_cfg.get("enabled", True))
    same_style_required = bool(multiline_cfg.get("same_style_required", True))
    max_gap = float(multiline_cfg.get("max_vertical_gap", 8.0))
    output_rows: List[Dict[str, Any]] = []
    line_review: List[Dict[str, Any]] = []
    ignored_lines: List[Dict[str, Any]] = []

    current_context: Dict[str, str] = {}
    i = 0
    while i < len(lines):
        line = lines[i]
        action, rule = classify_line(line, rules, total_pages)
        line_status = "used" if action != "unclassified" else "unclassified"
        line_reason = ""

        if action == "ignore":
            line_review.append({**line.as_dict(), "classification": "ignore", "rule": (rule or {}).get("name", ""), "notes": "ignored_by_rule"})
            ignored_lines.append({**line.as_dict(), "classification": "ignore", "rule": (rule or {}).get("name", "")})
            i += 1
            continue

        if action in {"set_main_header", "main_header"}:
            current_context["main_header"] = collapse_ws(line.text)
            # Reset lower hierarchy unless disabled.
            if not (rule or {}).get("keep_lower_context", False):
                for k in ["subheader_1", "subheader_2", "subheader_3"]:
                    current_context.pop(k, None)
            line_review.append({**line.as_dict(), "classification": "main_header", "rule": (rule or {}).get("name", ""), "notes": "sets current main_header"})
            i += 1
            continue

        if action in {"set_subheader_1", "subheader_1"}:
            current_context["subheader_1"] = collapse_ws(line.text)
            if not (rule or {}).get("keep_lower_context", False):
                for k in ["subheader_2", "subheader_3"]:
                    current_context.pop(k, None)
            line_review.append({**line.as_dict(), "classification": "subheader_1", "rule": (rule or {}).get("name", ""), "notes": "sets current subheader_1"})
            i += 1
            continue

        if action in {"set_subheader_2", "subheader_2"}:
            current_context["subheader_2"] = collapse_ws(line.text)
            if not (rule or {}).get("keep_lower_context", False):
                current_context.pop("subheader_3", None)
            line_review.append({**line.as_dict(), "classification": "subheader_2", "rule": (rule or {}).get("name", ""), "notes": "sets current subheader_2"})
            i += 1
            continue

        if action in {"set_subheader_3", "subheader_3"}:
            current_context["subheader_3"] = collapse_ws(line.text)
            line_review.append({**line.as_dict(), "classification": "subheader_3", "rule": (rule or {}).get("name", ""), "notes": "sets current subheader_3"})
            i += 1
            continue

        has_page = bool(extract_page_value(line.text, page_regex, page_position_default)[1])
        if action in {"emit_row", "item", "content_item"} or (action == "unclassified" and has_page and config.get("default_item_rule", True)):
            use_line = line
            used_merge = False
            # If current line has no page and next line has page, merge. Handles Electric Center wrapped rows.
            if not has_page and multiline_enabled and i + 1 < len(lines):
                nxt = lines[i + 1]
                nxt_action, _nxt_rule = classify_line(nxt, rules, total_pages)
                nxt_has_page = bool(extract_page_value(nxt.text, page_regex, page_position_default)[1])
                gap = nxt.y0 - line.y1
                ok_style = styles_compatible(line, nxt) if same_style_required else True
                if nxt.page == line.page and nxt.region_name == line.region_name and nxt_action not in {"ignore", "set_main_header", "main_header", "set_subheader_1", "subheader_1", "set_subheader_2", "subheader_2", "set_subheader_3", "subheader_3"} and nxt_has_page and gap <= max_gap and ok_style:
                    use_line = merge_lines(line, nxt)
                    used_merge = True
            row = build_row_from_item_line(use_line, current_context, rule, config)
            if used_merge:
                row["review_reason"] = ";".join(dedupe_preserve([row.get("review_reason", ""), "merged_multiline_item"]))
                row["confidence_status"] = "needs_review" if config.get("multiline_items", {}).get("mark_merged_rows_for_review", False) else row["confidence_status"]
                line_review.append({**line.as_dict(), "classification": "merged_item_part_1", "rule": (rule or {}).get("name", "default_item"), "notes": f"merged_with_line_{lines[i+1].line_id}"})
                line_review.append({**lines[i+1].as_dict(), "classification": "merged_item_part_2", "rule": (rule or {}).get("name", "default_item"), "notes": f"merged_with_line_{line.line_id}"})
                i += 2
            else:
                line_review.append({**line.as_dict(), "classification": "item", "rule": (rule or {}).get("name", "default_item"), "notes": "emitted row"})
                i += 1
            output_rows.append(row)
            continue

        # Potential wrapped first line: no page yet. If next line has a page, merge even if no item rule matched.
        if multiline_enabled and not has_page and i + 1 < len(lines):
            nxt = lines[i + 1]
            nxt_action, nxt_rule = classify_line(nxt, rules, total_pages)
            nxt_has_page = bool(extract_page_value(nxt.text, page_regex, page_position_default)[1])
            gap = nxt.y0 - line.y1
            ok_style = styles_compatible(line, nxt) if same_style_required else True
            if nxt.page == line.page and nxt.region_name == line.region_name and nxt_action not in {"ignore", "set_main_header", "main_header", "set_subheader_1", "subheader_1", "set_subheader_2", "subheader_2", "set_subheader_3", "subheader_3"} and nxt_has_page and gap <= max_gap and ok_style:
                use_line = merge_lines(line, nxt)
                row = build_row_from_item_line(use_line, current_context, nxt_rule, config)
                row["review_reason"] = ";".join(dedupe_preserve([row.get("review_reason", ""), "merged_multiline_item"]))
                output_rows.append(row)
                line_review.append({**line.as_dict(), "classification": "merged_item_part_1", "rule": (nxt_rule or {}).get("name", "default_item"), "notes": f"merged_with_line_{nxt.line_id}"})
                line_review.append({**nxt.as_dict(), "classification": "merged_item_part_2", "rule": (nxt_rule or {}).get("name", "default_item"), "notes": f"merged_with_line_{line.line_id}"})
                i += 2
                continue

        line_review.append({**line.as_dict(), "classification": line_status, "rule": (rule or {}).get("name", ""), "notes": line_reason})
        i += 1

    return output_rows, line_review, ignored_lines


def extract_lines_in_rect(page: fitz.Page, rect_cfg: Dict[str, Any], y_tolerance: float = 3.0) -> List[LineRecord]:
    rect = fitz.Rect(float(rect_cfg["x0"]), float(rect_cfg["y0"]), float(rect_cfg["x1"]), float(rect_cfg["y1"]))
    words = page.get_text("words", clip=rect, sort=True) or []
    if not words:
        return []
    clusters: List[Dict[str, Any]] = []
    for w in sorted(words, key=lambda w: ((float(w[1]) + float(w[3])) / 2, float(w[0]))):
        x0, y0, x1, y1, text = float(w[0]), float(w[1]), float(w[2]), float(w[3]), str(w[4])
        y_mid = (y0 + y1) / 2
        placed = False
        for c in clusters:
            if abs(float(c["y_mid"]) - y_mid) <= y_tolerance:
                c["words"].append((x0, y0, x1, y1, text))
                c["y_mid"] = sum((ww[1] + ww[3]) / 2 for ww in c["words"]) / len(c["words"])
                placed = True
                break
        if not placed:
            clusters.append({"y_mid": y_mid, "words": [(x0, y0, x1, y1, text)]})
    out: List[LineRecord] = []
    for idx, c in enumerate(sorted(clusters, key=lambda c: c["y_mid"]), start=1):
        ws = sorted(c["words"], key=lambda w: w[0])
        text = collapse_ws(" ".join(w[4] for w in ws))
        if not text:
            continue
        out.append(LineRecord(
            page=page.number + 1,
            line_id=idx,
            text=text,
            x0=min(w[0] for w in ws),
            y0=min(w[1] for w in ws),
            x1=max(w[2] for w in ws),
            y1=max(w[3] for w in ws),
            y_mid=sum((w[1] + w[3]) / 2 for w in ws) / len(ws),
        ))
    return out


def align_column_lines(column_lines: Dict[str, List[LineRecord]], row_tolerance: float = 3.0) -> List[Dict[str, Any]]:
    anchors: List[float] = []
    for field, lines in column_lines.items():
        for ln in lines:
            anchors.append(ln.y_mid)
    clusters: List[Dict[str, Any]] = []
    for y in sorted(anchors):
        placed = False
        for c in clusters:
            if abs(c["y_mid"] - y) <= row_tolerance:
                c["ys"].append(y)
                c["y_mid"] = sum(c["ys"]) / len(c["ys"])
                placed = True
                break
        if not placed:
            clusters.append({"y_mid": y, "ys": [y]})
    rows: List[Dict[str, Any]] = []
    for cidx, c in enumerate(clusters, start=1):
        row = {"row_number": cidx, "y_mid": c["y_mid"], "source_line_ids": []}
        for field, lines in column_lines.items():
            candidates = sorted(lines, key=lambda ln: abs(ln.y_mid - c["y_mid"]))
            if candidates and abs(candidates[0].y_mid - c["y_mid"]) <= row_tolerance:
                row[field] = candidates[0].text
                row["source_line_ids"].extend(candidates[0].source_line_ids or [candidates[0].line_id])
            else:
                row[field] = ""
        rows.append(row)
    return rows


def extract_area_table_rows(doc: fitz.Document, pages: Sequence[int], config: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    groups = config.get("area_table_groups", []) or []
    if not groups:
        return [], []
    output_rows: List[Dict[str, Any]] = []
    review_rows: List[Dict[str, Any]] = []
    row_tol_default = float(config.get("area_table", {}).get("row_tolerance", 3.0))
    page_cfg = config.get("page_number", {}) or {}
    regex = page_cfg.get("regex") or DEFAULT_PAGE_REGEX
    for pno in pages:
        page = doc[pno - 1]
        for group in groups:
            if not page_applies(pno, group.get("page_range") or group.get("apply_pdf_pages"), doc.page_count):
                continue
            col_lines: Dict[str, List[LineRecord]] = {}
            for col in group.get("columns", []) or []:
                field = col.get("field") or col.get("output_field")
                if not field:
                    continue
                col_lines[field] = extract_lines_in_rect(page, col, y_tolerance=float(group.get("line_y_tolerance", 3.0)))
            aligned = align_column_lines(col_lines, row_tolerance=float(group.get("row_tolerance", row_tol_default)))
            for ar in aligned:
                row: Dict[str, Any] = dict(group.get("fixed_context", {}) or {})
                for field, value in ar.items():
                    if field in {"row_number", "y_mid", "source_line_ids"}:
                        continue
                    row[field] = value
                # Optional page clean-up from separate page column or from item text.
                if row.get("page_number"):
                    row["page_number"] = normalize_page_value(row["page_number"])
                else:
                    # Try to detect page number in a text field if no separate page field.
                    for f in ["subheader_3", "subheader_2", "subheader_1", "item_text"]:
                        if row.get(f):
                            item, pg = extract_page_value(row[f], regex, page_cfg.get("position", "trailing"))
                            if pg:
                                row[f] = item
                                row["page_number"] = pg
                                break
                row.setdefault("main_header", "")
                row.setdefault("subheader_1", "")
                row.setdefault("subheader_2", "")
                row.setdefault("subheader_3", "")
                row.setdefault("page_number", "")
                row["source_pdf_page"] = pno
                row["source_region"] = group.get("name", "")
                row["source_line_ids"] = ";".join(str(x) for x in ar.get("source_line_ids", []))
                row["raw_text"] = " | ".join(str(v) for k, v in ar.items() if k not in {"row_number", "y_mid", "source_line_ids"} and v)
                row["classification_rule"] = "area_table_group"
                row["confidence_status"] = "confirmed" if row.get("page_number") else "needs_review"
                row["review_reason"] = "" if row.get("page_number") else "missing_page_number"
                output_rows.append(row)
                review_rows.append(dict(row))
    return output_rows, review_rows


def extract_card_grid_rows(doc: fitz.Document, pages: Sequence[int], config: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    card_cfg = config.get("card_grid", {}) or {}
    groups = card_cfg.get("groups", []) or []
    output_rows: List[Dict[str, Any]] = []
    review_rows: List[Dict[str, Any]] = []
    row_tol = float(card_cfg.get("row_tolerance", 10.0))
    default_pairing = card_cfg.get("pairing_direction", "right")
    for pno in pages:
        page = doc[pno - 1]
        for group in groups:
            if not page_applies(pno, group.get("page_range") or group.get("apply_pdf_pages"), doc.page_count):
                continue
            context = dict(group.get("fixed_context", {}) or {})
            # Full manual card list: each card has exact page/title region.
            if group.get("cards"):
                for cidx, card in enumerate(group.get("cards", []), start=1):
                    title_lines = []
                    page_lines = []
                    if card.get("title_region"):
                        title_lines = extract_lines_in_rect(page, card["title_region"], y_tolerance=float(card.get("line_y_tolerance", 3.0)))
                    if card.get("page_number_region"):
                        page_lines = extract_lines_in_rect(page, card["page_number_region"], y_tolerance=float(card.get("line_y_tolerance", 3.0)))
                    title = collapse_ws(" ".join(ln.text for ln in title_lines))
                    page_val = normalize_page_value(" ".join(ln.text for ln in page_lines))
                    row = dict(context)
                    row.update(card.get("fixed_context", {}) or {})
                    row.setdefault("main_header", "")
                    row.setdefault("subheader_1", title)
                    row.setdefault("subheader_2", "")
                    row.setdefault("subheader_3", "")
                    row["page_number"] = page_val
                    row["source_pdf_page"] = pno
                    row["source_region"] = group.get("name", "") + f"/card_{cidx}"
                    row["source_line_ids"] = ";".join(str(x) for ln in title_lines + page_lines for x in (ln.source_line_ids or [ln.line_id]))
                    row["raw_text"] = collapse_ws(page_val + " " + title)
                    row["classification_rule"] = "card_manual"
                    row["confidence_status"] = "confirmed" if title and page_val else "needs_review"
                    row["review_reason"] = "" if title and page_val else "missing_title_or_page"
                    output_rows.append(row)
                    review_rows.append(dict(row))
                continue

            # Region pairing: all page numbers in one or more regions, all titles in one or more regions.
            page_lines: List[LineRecord] = []
            title_lines: List[LineRecord] = []
            for preg in group.get("page_number_regions", []) or []:
                page_lines.extend(extract_lines_in_rect(page, preg, y_tolerance=float(group.get("line_y_tolerance", 3.0))))
            for treg in group.get("title_regions", []) or []:
                title_lines.extend(extract_lines_in_rect(page, treg, y_tolerance=float(group.get("line_y_tolerance", 3.0))))
            used_titles = set()
            for pline in sorted(page_lines, key=lambda ln: (ln.y_mid, ln.x0)):
                candidates = []
                for tline in title_lines:
                    if id(tline) in used_titles:
                        continue
                    dy = abs(tline.y_mid - pline.y_mid)
                    direction = group.get("pairing_direction", default_pairing)
                    ok_dir = True
                    if direction == "right":
                        ok_dir = tline.x0 >= pline.x1 - 5
                    elif direction == "left":
                        ok_dir = tline.x1 <= pline.x0 + 5
                    elif direction == "below":
                        ok_dir = tline.y0 >= pline.y1 - 5
                    elif direction == "above":
                        ok_dir = tline.y1 <= pline.y0 + 5
                    if ok_dir and dy <= float(group.get("max_y_distance", row_tol)):
                        dist = dy + abs(tline.x0 - pline.x1) / 1000
                        candidates.append((dist, tline))
                if not candidates:
                    row = dict(context)
                    row.setdefault("main_header", "")
                    row.setdefault("subheader_1", "")
                    row.setdefault("subheader_2", "")
                    row.setdefault("subheader_3", "")
                    row["page_number"] = normalize_page_value(pline.text)
                    row["source_pdf_page"] = pno
                    row["source_region"] = group.get("name", "")
                    row["source_line_ids"] = ";".join(str(x) for x in (pline.source_line_ids or [pline.line_id]))
                    row["raw_text"] = pline.text
                    row["classification_rule"] = "card_grid_pairing"
                    row["confidence_status"] = "needs_review"
                    row["review_reason"] = "page_number_without_matching_title"
                    output_rows.append(row)
                    review_rows.append(dict(row))
                    continue
                tline = sorted(candidates, key=lambda x: x[0])[0][1]
                used_titles.add(id(tline))
                row = dict(context)
                row.setdefault("main_header", "")
                row["subheader_1"] = tline.text
                row.setdefault("subheader_2", "")
                row.setdefault("subheader_3", "")
                row["page_number"] = normalize_page_value(pline.text)
                row["source_pdf_page"] = pno
                row["source_region"] = group.get("name", "")
                row["source_line_ids"] = ";".join(str(x) for ln in [pline, tline] for x in (ln.source_line_ids or [ln.line_id]))
                row["raw_text"] = collapse_ws(pline.text + " " + tline.text)
                row["classification_rule"] = "card_grid_pairing"
                row["confidence_status"] = "confirmed"
                row["review_reason"] = ""
                output_rows.append(row)
                review_rows.append(dict(row))
    return output_rows, review_rows


def derive_page_ranges(rows: List[Dict[str, Any]], config: Dict[str, Any]) -> List[Dict[str, Any]]:
    cfg = config.get("page_range_derivation", {}) or {}
    if not bool(cfg.get("enabled", False)):
        return rows
    scope_cols = cfg.get("scope_columns") or ["main_header", "subheader_1"]
    same_rule_only = bool(cfg.get("same_rule_only", False))
    # Preserve extraction order.
    grouped: Dict[Tuple[Any, ...], List[int]] = defaultdict(list)
    for idx, row in enumerate(rows):
        key_parts = [row.get(c, "") for c in scope_cols]
        if same_rule_only:
            key_parts.append(row.get("classification_rule", ""))
        grouped[tuple(key_parts)].append(idx)
    for idxs in grouped.values():
        for pos, idx in enumerate(idxs):
            row = rows[idx]
            current = row.get("page_number", "")
            if not current or not re.fullmatch(r"\d+", str(current).strip()):
                continue
            if pos + 1 < len(idxs):
                nxt = rows[idxs[pos + 1]].get("page_number", "")
                row["page_number"] = format_derived_range(current, nxt)
                row["review_reason"] = ";".join(dedupe_preserve([row.get("review_reason", ""), "page_range_derived_from_next_item"]))
            elif cfg.get("last_item_strategy", "single_page") == "single_page":
                row["page_number"] = normalize_page_value(current)
    return rows


def standard_output_columns(config: Dict[str, Any]) -> List[str]:
    cols = config.get("output_columns") or config.get("outputs", {}).get("columns")
    if not cols:
        cols = ["main_header", "subheader_1", "subheader_2", "subheader_3", "page_number"]
    normalized = [OUTPUT_ALIASES.get(c, c) for c in cols]
    return normalized


def finalise_rows(rows: List[Dict[str, Any]], config: Dict[str, Any]) -> List[Dict[str, Any]]:
    out_cols = standard_output_columns(config)
    for row in rows:
        for col in out_cols:
            row.setdefault(col, "")
        row["page_number"] = normalize_page_value(row.get("page_number", ""))
        # Requested combined helper for EC-like configs.
        if "subheader_2_page_number_combined" in out_cols and not row.get("subheader_2_page_number_combined"):
            sub = row.get("subheader_2") or row.get("subheader_1") or row.get("subheader_3") or ""
            pg = row.get("page_number", "")
            row["subheader_2_page_number_combined"] = collapse_ws(f"{sub} {pg}") if pg else sub
    return rows


def validate_examples(rows: List[Dict[str, Any]], config: Dict[str, Any]) -> List[Dict[str, Any]]:
    examples = config.get("validation_examples", []) or []
    results: List[Dict[str, Any]] = []
    for ex in examples:
        matched = False
        mismatch_notes: List[str] = []
        for row in rows:
            ok = True
            for k, v in ex.items():
                if k in {"notes", "description"}:
                    continue
                if normalize_page_value(row.get(k, "")) != normalize_page_value(v):
                    ok = False
                    break
            if ok:
                matched = True
                break
        if not matched:
            # Provide simple diagnostics for first missing field.
            mismatch_notes.append("expected_combination_not_found")
        results.append({**ex, "status": "found" if matched else "not_found", "review_reason": ";".join(mismatch_notes)})
    return results


def build_registry_rows(rows: List[Dict[str, Any]], config: Dict[str, Any]) -> List[Dict[str, Any]]:
    out_cols = standard_output_columns(config)
    seen = set()
    registry: List[Dict[str, Any]] = []
    for row in rows:
        key = tuple(collapse_ws(row.get(c, "")) for c in out_cols)
        if not row.get("page_number"):
            continue
        if key in seen:
            continue
        seen.add(key)
        registry.append({col: row.get(col, "") for col in out_cols})
    return registry


def write_csv(path: Path, rows: List[Dict[str, Any]], fields: Optional[List[str]] = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if fields is None:
        fields = []
        seen = set()
        for r in rows:
            for k in r.keys():
                if k not in seen:
                    seen.add(k)
                    fields.append(k)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for r in rows:
            writer.writerow({k: r.get(k, "") for k in fields})


def safe_sheet_name(name: str) -> str:
    for ch in '[]:*?/\\':
        name = name.replace(ch, "_")
    return name[:31]


def write_review_workbook(path: Path, sheets: Dict[str, List[Dict[str, Any]]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        for name, rows in sheets.items():
            df = pd.DataFrame(rows)
            if df.empty:
                df = pd.DataFrame([{"message": "No records"}])
            sheet_name = safe_sheet_name(name)
            df.to_excel(writer, sheet_name=sheet_name, index=False)
            ws = writer.book[sheet_name]
            ws.freeze_panes = "A2"
            for col_cells in ws.columns:
                max_len = min(max(len(str(c.value)) if c.value is not None else 0 for c in col_cells), 80)
                ws.column_dimensions[col_cells[0].column_letter].width = max(12, max_len + 2)


def make_debug_images(doc: fitz.Document, pages: Sequence[int], output_folder: Path, rows: List[Dict[str, Any]], config: Dict[str, Any]) -> None:
    if Image is None or not config.get("debug_images", {}).get("enabled", True):
        return
    debug_dir = output_folder / "debug_images"
    debug_dir.mkdir(parents=True, exist_ok=True)
    zoom = float(config.get("debug_images", {}).get("zoom", 1.5))
    issue_pages = {int(r.get("source_pdf_page")) for r in rows if r.get("confidence_status") == "needs_review" and r.get("source_pdf_page")}
    validation_failed = [v for v in config.get("_validation_results", []) if v.get("status") != "found"]
    if validation_failed:
        issue_pages.update(pages)
    if not issue_pages and config.get("debug_images", {}).get("only_issue_pages", True):
        return
    draw_pages = sorted(issue_pages or set(pages))
    font = ImageFont.load_default()
    for pno in draw_pages:
        if pno < 1 or pno > doc.page_count:
            continue
        page = doc[pno - 1]
        pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
        tmp = debug_dir / f"_tmp_{pno}.png"
        pix.save(str(tmp))
        img = Image.open(tmp).convert("RGB")
        draw = ImageDraw.Draw(img)
        # Draw configured regions.
        region_sets = []
        for r in config.get("content_regions", []) or []:
            if page_applies(pno, r.get("page_range") or r.get("apply_pdf_pages"), doc.page_count):
                region_sets.append((r, (0, 0, 255), r.get("name", "content")))
        for r in config.get("ignore_regions", []) or []:
            if page_applies(pno, r.get("page_range") or r.get("apply_pdf_pages"), doc.page_count):
                region_sets.append((r, (255, 0, 0), r.get("name", "ignore")))
        for g in config.get("area_table_groups", []) or []:
            if page_applies(pno, g.get("page_range") or g.get("apply_pdf_pages"), doc.page_count):
                for c in g.get("columns", []) or []:
                    region_sets.append((c, (0, 128, 0), f"{g.get('name','area')}:{c.get('field','')}") )
        for g in (config.get("card_grid", {}) or {}).get("groups", []) or []:
            if page_applies(pno, g.get("page_range") or g.get("apply_pdf_pages"), doc.page_count):
                for r in g.get("page_number_regions", []) or []:
                    region_sets.append((r, (128, 0, 128), f"{g.get('name','card')}:page"))
                for r in g.get("title_regions", []) or []:
                    region_sets.append((r, (255, 128, 0), f"{g.get('name','card')}:title"))
        for r, color, label in region_sets:
            try:
                x0, y0, x1, y1 = [float(v) * zoom for v in rect_from_cfg(r)]
                draw.rectangle([x0, y0, x1, y1], outline=color, width=2)
                draw.text((x0, max(0, y0 - 12)), label, fill=color, font=font)
            except Exception:
                continue
        img.save(debug_dir / f"contents_debug_page_{pno:03d}.png")
        try:
            tmp.unlink()
        except Exception:
            pass


def run_extraction(config: Dict[str, Any]) -> Dict[str, Any]:
    config = load_visual_templates_into_config(config)
    input_pdf = Path(config["input_pdf"])
    output_folder = Path(config.get("output_folder", input_pdf.with_name("contents_output")))
    output_folder.mkdir(parents=True, exist_ok=True)
    if not input_pdf.exists():
        raise SystemExit(f"PDF not found: {input_pdf}")
    log(f"Opening PDF: {input_pdf}")
    doc = fitz.open(str(input_pdf))
    pages = parse_page_range(config.get("contents_pdf_pages") or config.get("index_pdf_pages") or "all", doc.page_count)
    mode = (config.get("extraction", {}) or {}).get("mode") or config.get("extraction_mode") or "style_rules"
    log(f"PDF has {doc.page_count} pages. Processing {len(pages)} contents page(s) in mode: {mode}")

    all_rows: List[Dict[str, Any]] = []
    line_review: List[Dict[str, Any]] = []
    ignored_lines: List[Dict[str, Any]] = []
    errors: List[Dict[str, Any]] = []

    try:
        if mode in {"style_rules", "region_rules"}:
            style_rows, lr, ign = extract_style_or_region_rows(doc, pages, config)
            all_rows.extend(style_rows)
            line_review.extend(lr)
            ignored_lines.extend(ign)
            area_rows, area_review = extract_area_table_rows(doc, pages, config)
            all_rows.extend(area_rows)
            line_review.extend(area_review)
        elif mode == "card_grid":
            card_rows, card_review = extract_card_grid_rows(doc, pages, config)
            all_rows.extend(card_rows)
            line_review.extend(card_review)
        elif mode == "area_table":
            area_rows, area_review = extract_area_table_rows(doc, pages, config)
            all_rows.extend(area_rows)
            line_review.extend(area_review)
        else:
            raise ValueError(f"Unknown extraction mode: {mode}")
    except Exception as exc:
        errors.append({"error": repr(exc)})
        log(f"Extraction error: {exc!r}")

    all_rows = finalise_rows(all_rows, config)
    all_rows = derive_page_ranges(all_rows, config)
    all_rows = finalise_rows(all_rows, config)
    validation = validate_examples(all_rows, config)
    config["_validation_results"] = validation
    registry = build_registry_rows(all_rows, config)

    out_cols = standard_output_columns(config)
    write_csv(output_folder / "contents.registry.csv", registry, fields=out_cols)
    rows_fields = out_cols + [
        "source_pdf_page", "source_region", "source_line_ids", "raw_text", "classification_rule",
        "confidence_status", "review_reason", "row_type"
    ]
    write_csv(output_folder / "contents_rows.csv", all_rows, fields=rows_fields)

    page_diag: List[Dict[str, Any]] = []
    for p in pages:
        rows_on_page = [r for r in all_rows if int(r.get("source_pdf_page") or -1) == p]
        page_diag.append({
            "source_pdf_page": p,
            "row_count": len(rows_on_page),
            "needs_review_count": sum(1 for r in rows_on_page if r.get("confidence_status") == "needs_review"),
            "regions_or_groups_used": ";".join(dedupe_preserve(r.get("source_region", "") for r in rows_on_page)),
            "status": "ok" if rows_on_page else "no_rows_extracted",
        })

    run_summary = [
        {"metric": "extraction_mode", "value": mode},
        {"metric": "contents_pages_processed", "value": len(pages)},
        {"metric": "contents_rows", "value": len(all_rows)},
        {"metric": "registry_rows", "value": len(registry)},
        {"metric": "rows_needing_review", "value": sum(1 for r in all_rows if r.get("confidence_status") == "needs_review")},
        {"metric": "validation_examples", "value": len(validation)},
        {"metric": "validation_failures", "value": sum(1 for v in validation if v.get("status") != "found")},
        {"metric": "errors", "value": len(errors)},
    ]

    review_sheets = {
        "Run Summary": run_summary,
        "Page Diagnostics": page_diag,
        "Extracted Contents Review": all_rows,
        "Unresolved Rows": [r for r in all_rows if r.get("confidence_status") == "needs_review"],
        "Line Classification": line_review,
        "Rule Matches": [r for r in line_review if r.get("classification") not in {"unclassified", "ignore"}],
        "Validation Examples": validation,
        "Ignored Lines": ignored_lines,
        "Extractor Errors": errors,
    }
    write_review_workbook(output_folder / "contents_review_workbook.xlsx", review_sheets)
    save_config({k: v for k, v in config.items() if k != "_validation_results"}, output_folder / "contents_config_used.yaml")
    make_debug_images(doc, pages, output_folder, all_rows, config)
    doc.close()
    log(f"Extraction complete. Main output: {output_folder / 'contents.registry.csv'}")
    return {"rows": all_rows, "registry": registry, "validation": validation, "errors": errors}


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Extract structured catalogue contents rows from selectable-text PDF contents pages.")
    parser.add_argument("--config", required=True, help="Path to YAML/JSON contents extraction config.")
    parser.add_argument("--input", help="Override input_pdf from config.")
    parser.add_argument("--pages", help="Override contents_pdf_pages from config.")
    parser.add_argument("--output", help="Override output_folder from config.")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> None:
    args = parse_args(argv)
    config = load_config(Path(args.config))
    if args.input:
        config["input_pdf"] = args.input
    if args.pages:
        config["contents_pdf_pages"] = args.pages
    if args.output:
        config["output_folder"] = args.output
    run_extraction(config)


if __name__ == "__main__":
    main()
