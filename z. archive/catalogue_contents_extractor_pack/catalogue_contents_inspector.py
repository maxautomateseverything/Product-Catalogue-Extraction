#!/usr/bin/env python3
"""
Catalogue Contents Inspector
----------------------------
Run this before configuring the contents extractor.

It inspects selectable-text PDF contents pages and creates a workbook showing:
- document metadata
- page summary
- line records
- span records with font / size / colour / direction metadata
- word records
- font and colour summaries
- suggested extraction rules
- optional debug images with line boxes and labels

Usage:
  py catalogue_contents_inspector.py --config contents_config.yaml
  py catalogue_contents_inspector.py --input catalogue.pdf --pages 13-14 --output ContentsInspection
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from statistics import median
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

try:
    import fitz  # PyMuPDF
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
    from PIL import ImageDraw, ImageFont
except ImportError:  # pragma: no cover
    ImageDraw = None
    ImageFont = None


DEFAULT_PAGE_REGEX = r"(?i)(?:see\s+page\s+)?[A-Z]?\d+(?:\s*(?:,|/|;|-|–|—)\s*[A-Z]?\d+)*"


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


def parse_page_range(page_range: str, total_pages: int) -> List[int]:
    raw = str(page_range or "").strip().lower()
    if not raw:
        raise ValueError("Page range is blank.")
    if raw == "all":
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
            if start > end:
                raise ValueError(f"Invalid page range: {part}")
            pages.extend(range(start, end + 1))
        else:
            pages.append(int(part))
    seen = set()
    ordered: List[int] = []
    invalid: List[int] = []
    for p in pages:
        if p < 1 or p > total_pages:
            invalid.append(p)
            continue
        if p not in seen:
            seen.add(p)
            ordered.append(p)
    if invalid:
        log(f"Warning: ignoring invalid page numbers: {invalid}")
    return ordered


def clean_pdf_date(value: Any) -> Optional[str]:
    if not value:
        return None
    value = str(value)
    m = re.match(r"D:(\d{4})(\d{2})?(\d{2})?(\d{2})?(\d{2})?(\d{2})?", value)
    if not m:
        return value
    y, mo, d, h, mi, s = m.groups()
    return f"{y}-{mo or '01'}-{d or '01'} {h or '00'}:{mi or '00'}:{s or '00'}"


def srgb_to_hex(value: Any) -> str:
    try:
        ivalue = int(value or 0)
        return f"#{ivalue & 0xFFFFFF:06X}"
    except Exception:
        return ""


def flags_to_style(flags: Any, font: str = "") -> str:
    try:
        flags_i = int(flags or 0)
    except Exception:
        flags_i = 0
    parts: List[str] = []
    # PyMuPDF flags vary by font; names are heuristic for user review.
    if flags_i & 2:
        parts.append("italic")
    if flags_i & 16:
        parts.append("bold")
    font_l = str(font or "").lower()
    if "bold" in font_l and "bold" not in parts:
        parts.append("bold_fontname")
    if "italic" in font_l or "oblique" in font_l:
        if "italic" not in parts:
            parts.append("italic_fontname")
    return ";".join(parts)


def rect_intersects(a: Tuple[float, float, float, float], b: Tuple[float, float, float, float]) -> bool:
    ax0, ay0, ax1, ay1 = a
    bx0, by0, bx1, by1 = b
    return not (ax1 < bx0 or bx1 < ax0 or ay1 < by0 or by1 < ay0)


def extract_document_metadata(pdf_path: Path) -> List[Dict[str, Any]]:
    doc = fitz.open(str(pdf_path))
    meta = dict(doc.metadata or {})
    rows = [
        {"field": "file_name", "value": pdf_path.name},
        {"field": "file_path", "value": str(pdf_path.resolve())},
        {"field": "file_size_bytes", "value": pdf_path.stat().st_size},
        {"field": "page_count", "value": doc.page_count},
        {"field": "is_encrypted", "value": doc.is_encrypted},
        {"field": "needs_pass", "value": doc.needs_pass},
        {"field": "creationDate_cleaned", "value": clean_pdf_date(meta.get("creationDate"))},
        {"field": "modDate_cleaned", "value": clean_pdf_date(meta.get("modDate"))},
    ]
    for k, v in meta.items():
        rows.append({"field": f"metadata.{k}", "value": v})
    doc.close()
    return rows


def extract_page_records(doc: fitz.Document, pages: Sequence[int], text_preview_chars: int) -> List[Dict[str, Any]]:
    records: List[Dict[str, Any]] = []
    for pno in pages:
        page = doc[pno - 1]
        text = page.get_text("text", sort=True) or ""
        blocks = page.get_text("blocks") or []
        words = page.get_text("words", sort=True) or []
        raw = page.get_text("dict") or {}
        spans = 0
        lines = 0
        image_blocks = 0
        for block in raw.get("blocks", []):
            if block.get("type") == 1:
                image_blocks += 1
            for line in block.get("lines", []) or []:
                lines += 1
                spans += len(line.get("spans", []) or [])
        records.append({
            "page": pno,
            "width": round(float(page.rect.width), 3),
            "height": round(float(page.rect.height), 3),
            "rotation": page.rotation,
            "has_selectable_text": bool(text.strip()),
            "text_char_count": len(text),
            "word_count": len(words),
            "block_count": len(blocks),
            "line_count": lines,
            "span_count": spans,
            "image_block_count": image_blocks,
            "text_preview": text[:text_preview_chars].replace("\n", " "),
        })
    return records


def extract_span_line_word_records(doc: fitz.Document, pages: Sequence[int], page_regex: str) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]]]:
    span_records: List[Dict[str, Any]] = []
    line_records: List[Dict[str, Any]] = []
    word_records: List[Dict[str, Any]] = []
    page_re = re.compile(page_regex or DEFAULT_PAGE_REGEX)

    for pno in pages:
        page = doc[pno - 1]
        raw = page.get_text("dict") or {}
        line_id = 0
        span_id = 0
        for block_idx, block in enumerate(raw.get("blocks", []) or [], start=1):
            if block.get("type") != 0:
                continue
            for pdf_line_idx, line in enumerate(block.get("lines", []) or [], start=1):
                spans = line.get("spans", []) or []
                span_texts = [str(s.get("text", "")) for s in spans if str(s.get("text", "")).strip()]
                text = " ".join(t.strip() for t in span_texts if t.strip()).strip()
                if not text:
                    continue
                line_id += 1
                bboxes = [s.get("bbox") for s in spans if s.get("bbox")]
                x0 = min(float(b[0]) for b in bboxes)
                y0 = min(float(b[1]) for b in bboxes)
                x1 = max(float(b[2]) for b in bboxes)
                y1 = max(float(b[3]) for b in bboxes)
                sizes = [float(s.get("size", 0)) for s in spans if s.get("size") is not None]
                fonts = [str(s.get("font", "")) for s in spans if s.get("font")]
                colours = [srgb_to_hex(s.get("color")) for s in spans]
                flags = [s.get("flags") for s in spans]
                direction = line.get("dir", (None, None))
                page_matches = page_re.findall(text)
                line_records.append({
                    "page": pno,
                    "line_id": line_id,
                    "block_id": block_idx,
                    "pdf_line_id": pdf_line_idx,
                    "text": text,
                    "x0": round(x0, 3),
                    "y0": round(y0, 3),
                    "x1": round(x1, 3),
                    "y1": round(y1, 3),
                    "width": round(x1 - x0, 3),
                    "height": round(y1 - y0, 3),
                    "y_mid": round((y0 + y1) / 2, 3),
                    "direction_x": direction[0] if isinstance(direction, (list, tuple)) and len(direction) > 0 else "",
                    "direction_y": direction[1] if isinstance(direction, (list, tuple)) and len(direction) > 1 else "",
                    "dominant_font": Counter(fonts).most_common(1)[0][0] if fonts else "",
                    "font_names": "; ".join(sorted(set(fonts))),
                    "median_font_size": round(median(sizes), 3) if sizes else "",
                    "min_font_size": round(min(sizes), 3) if sizes else "",
                    "max_font_size": round(max(sizes), 3) if sizes else "",
                    "colors": "; ".join(sorted(set(colours))),
                    "dominant_color": Counter(colours).most_common(1)[0][0] if colours else "",
                    "styles": "; ".join(sorted(set(flags_to_style(f, fonts[i] if i < len(fonts) else "") for i, f in enumerate(flags)))),
                    "contains_page_number_candidate": bool(page_matches),
                    "page_number_candidates": "; ".join([m if isinstance(m, str) else " ".join(m) for m in page_matches[:10]]),
                    "is_uppercase_like": bool(re.search(r"[A-Z]", text)) and text.upper() == text,
                })
                for sidx, span in enumerate(spans, start=1):
                    stext = str(span.get("text", ""))
                    if not stext.strip():
                        continue
                    span_id += 1
                    sb = span.get("bbox", [None, None, None, None])
                    span_records.append({
                        "page": pno,
                        "line_id": line_id,
                        "span_id": span_id,
                        "block_id": block_idx,
                        "span_index_in_line": sidx,
                        "text": stext,
                        "x0": round(float(sb[0]), 3),
                        "y0": round(float(sb[1]), 3),
                        "x1": round(float(sb[2]), 3),
                        "y1": round(float(sb[3]), 3),
                        "width": round(float(sb[2]) - float(sb[0]), 3),
                        "height": round(float(sb[3]) - float(sb[1]), 3),
                        "font": span.get("font", ""),
                        "size": round(float(span.get("size", 0)), 3),
                        "flags": span.get("flags", ""),
                        "style_hint": flags_to_style(span.get("flags"), span.get("font", "")),
                        "color": srgb_to_hex(span.get("color")),
                        "origin_x": round(float((span.get("origin") or [0, 0])[0]), 3) if span.get("origin") else "",
                        "origin_y": round(float((span.get("origin") or [0, 0])[1]), 3) if span.get("origin") else "",
                    })
        for widx, word in enumerate(page.get_text("words", sort=True) or [], start=1):
            # PyMuPDF tuple: x0, y0, x1, y1, word, block_no, line_no, word_no
            x0, y0, x1, y1, wtext, block_no, line_no, word_no = word[:8]
            word_records.append({
                "page": pno,
                "word_id": widx,
                "text": wtext,
                "x0": round(float(x0), 3),
                "y0": round(float(y0), 3),
                "x1": round(float(x1), 3),
                "y1": round(float(y1), 3),
                "width": round(float(x1) - float(x0), 3),
                "height": round(float(y1) - float(y0), 3),
                "y_mid": round((float(y0) + float(y1)) / 2, 3),
                "block_no": block_no,
                "line_no": line_no,
                "word_no": word_no,
            })
    return line_records, span_records, word_records


def build_font_summary(span_records: List[Dict[str, Any]], line_records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    counter: Counter = Counter()
    line_counter: Counter = Counter()
    examples: Dict[Tuple[str, float, str], str] = {}
    for s in span_records:
        key = (s.get("font", ""), float(s.get("size") or 0), s.get("color", ""))
        counter[key] += len(str(s.get("text", "")))
        examples.setdefault(key, str(s.get("text", ""))[:100])
    for ln in line_records:
        key = (ln.get("dominant_font", ""), float(ln.get("median_font_size") or 0), ln.get("dominant_color", ""))
        line_counter[key] += 1
    rows = []
    for (font, size, color), char_count in counter.most_common():
        rows.append({
            "font": font,
            "size": size,
            "color": color,
            "char_count": char_count,
            "line_count": line_counter.get((font, size, color), 0),
            "example_text": examples.get((font, size, color), ""),
        })
    return rows


def build_colour_summary(span_records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    counter = Counter(str(s.get("color", "")) for s in span_records)
    examples: Dict[str, str] = {}
    for s in span_records:
        examples.setdefault(str(s.get("color", "")), str(s.get("text", ""))[:100])
    return [{"color": c, "span_count": n, "example_text": examples.get(c, "")} for c, n in counter.most_common()]


def suggest_rules(line_records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    if not line_records:
        return rows

    # Page-number item candidates
    page_lines = [r for r in line_records if r.get("contains_page_number_candidate")]
    if page_lines:
        styles = Counter((r.get("dominant_font"), r.get("median_font_size"), r.get("dominant_color")) for r in page_lines)
        for idx, ((font, size, color), count) in enumerate(styles.most_common(10), start=1):
            example = next((r["text"] for r in page_lines if (r.get("dominant_font"), r.get("median_font_size"), r.get("dominant_color")) == (font, size, color)), "")
            rows.append({
                "suggestion_type": "possible_item_rule",
                "priority": 100 + idx,
                "reason": "Lines with this style often contain page-number candidates.",
                "fontname_contains": font,
                "font_size_min": float(size) - 0.25 if size != "" else "",
                "font_size_max": float(size) + 0.25 if size != "" else "",
                "color": color,
                "example_text": example,
                "rule_yaml_hint": f"- name: item_style_{idx}\n  action: emit_row\n  fontname_contains: '{font}'\n  font_size_min: {float(size) - 0.25 if size != '' else ''}\n  font_size_max: {float(size) + 0.25 if size != '' else ''}\n  page_number_position: trailing",
            })

    # Big / coloured / uppercase likely headers
    sizes = [float(r.get("median_font_size") or 0) for r in line_records if r.get("median_font_size") not in ("", None)]
    if sizes:
        q_size = sorted(sizes)[max(0, int(len(sizes) * 0.75) - 1)]
        header_candidates = [
            r for r in line_records
            if float(r.get("median_font_size") or 0) >= q_size
            and len(str(r.get("text", "")).strip()) >= 3
            and not r.get("contains_page_number_candidate")
        ]
        for idx, r in enumerate(header_candidates[:20], start=1):
            rows.append({
                "suggestion_type": "possible_header_rule",
                "priority": 10 + idx,
                "reason": "Large/non-page-number line. Review whether it should be main_header or subheader.",
                "fontname_contains": r.get("dominant_font", ""),
                "font_size_min": float(r.get("median_font_size") or 0) - 0.25,
                "font_size_max": float(r.get("median_font_size") or 0) + 0.25,
                "color": r.get("dominant_color", ""),
                "example_text": r.get("text", ""),
                "rule_yaml_hint": f"- name: header_style_{idx}\n  action: set_main_header\n  fontname_contains: '{r.get('dominant_font', '')}'\n  font_size_min: {float(r.get('median_font_size') or 0) - 0.25}\n  font_size_max: {float(r.get('median_font_size') or 0) + 0.25}",
            })
    return rows


def make_debug_images(doc: fitz.Document, pages: Sequence[int], line_records: List[Dict[str, Any]], out_dir: Path, zoom: float = 1.5, max_label_lines: int = 250) -> None:
    if ImageDraw is None:
        log("Pillow not available; skipping debug images.")
        return
    debug_dir = out_dir / "inspection_debug_images"
    debug_dir.mkdir(parents=True, exist_ok=True)
    by_page: Dict[int, List[Dict[str, Any]]] = defaultdict(list)
    for r in line_records:
        by_page[int(r["page"])].append(r)
    for pno in pages:
        page = doc[pno - 1]
        pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
        img_path_tmp = debug_dir / f"_tmp_page_{pno}.png"
        pix.save(str(img_path_tmp))
        from PIL import Image
        image = Image.open(img_path_tmp).convert("RGB")
        draw = ImageDraw.Draw(image)
        font = ImageFont.load_default()
        for r in by_page.get(pno, [])[:max_label_lines]:
            x0, y0, x1, y1 = [float(r[k]) * zoom for k in ("x0", "y0", "x1", "y1")]
            draw.rectangle([x0, y0, x1, y1], outline=(255, 0, 0), width=1)
            label = f"L{r['line_id']} sz={r.get('median_font_size','')} {r.get('dominant_color','')}"
            draw.text((x0, max(0, y0 - 10)), label, fill=(0, 0, 255), font=font)
        out_path = debug_dir / f"page_{pno:03d}_line_debug.png"
        image.save(out_path)
        try:
            img_path_tmp.unlink()
        except Exception:
            pass


def safe_sheet_name(name: str) -> str:
    bad = '[]:*?/\\'
    for ch in bad:
        name = name.replace(ch, "_")
    return name[:31]


def write_workbook(path: Path, sheets: Dict[str, List[Dict[str, Any]]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        for name, rows in sheets.items():
            df = pd.DataFrame(rows)
            if df.empty:
                df = pd.DataFrame([{"message": "No records"}])
            df.to_excel(writer, sheet_name=safe_sheet_name(name), index=False)
            ws = writer.book[safe_sheet_name(name)]
            ws.freeze_panes = "A2"
            for col_cells in ws.columns:
                max_len = min(max(len(str(c.value)) if c.value is not None else 0 for c in col_cells), 80)
                ws.column_dimensions[col_cells[0].column_letter].width = max(12, max_len + 2)


def run_inspection(config: Dict[str, Any]) -> Dict[str, Any]:
    input_pdf = Path(config["input_pdf"])
    output_folder = Path(config.get("output_folder", input_pdf.with_name("contents_inspection_output")))
    output_folder.mkdir(parents=True, exist_ok=True)
    inspection_cfg = config.get("inspection", {}) or {}
    page_range = config.get("contents_pdf_pages") or config.get("pages_to_inspect") or inspection_cfg.get("pages")
    text_preview_chars = int(inspection_cfg.get("text_preview_chars", 1000))
    page_regex = config.get("page_number", {}).get("regex") or DEFAULT_PAGE_REGEX
    debug_images = bool(inspection_cfg.get("debug_images", True))
    debug_zoom = float(inspection_cfg.get("debug_zoom", 1.5))

    if not input_pdf.exists():
        raise SystemExit(f"PDF not found: {input_pdf}")

    log(f"Opening PDF: {input_pdf}")
    doc = fitz.open(str(input_pdf))
    pages = parse_page_range(str(page_range), doc.page_count)
    log(f"PDF has {doc.page_count} pages. Inspecting {len(pages)} contents page(s): {pages}")

    metadata_rows = extract_document_metadata(input_pdf)
    page_summary = extract_page_records(doc, pages, text_preview_chars)
    line_records, span_records, word_records = extract_span_line_word_records(doc, pages, page_regex)
    font_summary = build_font_summary(span_records, line_records)
    colour_summary = build_colour_summary(span_records)
    suggested_rules = suggest_rules(line_records)

    sheets = {
        "Document Metadata": metadata_rows,
        "Page Summary": page_summary,
        "Font Summary": font_summary,
        "Colour Summary": colour_summary,
        "Suggested Rules": suggested_rules,
        "Line Records": line_records,
        "Span Records": span_records,
        "Word Records": word_records,
    }
    workbook_path = output_folder / "contents_inspection_workbook.xlsx"
    write_workbook(workbook_path, sheets)
    log(f"Created: {workbook_path}")

    # Helpful CSV mirrors for large data / easy filtering.
    if bool(inspection_cfg.get("write_csv_copies", False)):
        for sheet_name, rows in sheets.items():
            pd.DataFrame(rows).to_csv(output_folder / f"{sheet_name.lower().replace(' ', '_')}.csv", index=False, encoding="utf-8-sig")

    if debug_images:
        make_debug_images(doc, pages, line_records, output_folder, zoom=debug_zoom)
        log(f"Created debug images in: {output_folder / 'inspection_debug_images'}")

    doc.close()
    return {
        "workbook": str(workbook_path),
        "pages": pages,
        "line_count": len(line_records),
        "span_count": len(span_records),
        "word_count": len(word_records),
    }


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Inspect selectable-text PDF catalogue contents pages.")
    parser.add_argument("--config", help="Path to YAML/JSON config file.")
    parser.add_argument("--input", help="PDF path override / direct mode input.")
    parser.add_argument("--pages", help="1-based pages to inspect, e.g. 13-14 or 1,3,5.")
    parser.add_argument("--output", help="Output folder override / direct mode output.")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> None:
    args = parse_args(argv)
    if args.config:
        config = load_config(Path(args.config))
    else:
        if not args.input or not args.pages or not args.output:
            raise SystemExit("Provide --config, or provide --input, --pages, and --output.")
        config = {"input_pdf": args.input, "contents_pdf_pages": args.pages, "output_folder": args.output, "inspection": {"debug_images": True}}
    if args.input:
        config["input_pdf"] = args.input
    if args.pages:
        config["contents_pdf_pages"] = args.pages
    if args.output:
        config["output_folder"] = args.output
    run_inspection(config)


if __name__ == "__main__":
    main()
