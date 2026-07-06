#!/usr/bin/env python3
"""Visual PDF column selector for catalogue_index_extractor.py.

This script is a coordinate/template builder. It renders a PDF page as an image,
lets the user drag-select table-column rectangles, and saves those rectangles in
PDF-native coordinates. Extraction still happens from the original PDF text layer
inside catalogue_index_extractor.py.

Typical usage
-------------
py catalogue_region_selector.py --pdf catalogue.pdf --config catalogue_index_config.yaml --page 1312 --output-template layout_1_template.json --output-config catalogue_index_config_layout_1.yaml
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

try:
    import fitz  # PyMuPDF
except ImportError as exc:  # pragma: no cover
    raise SystemExit("Missing dependency: PyMuPDF. Install with: py -m pip install PyMuPDF") from exc

try:
    from PIL import Image, ImageTk
except ImportError as exc:  # pragma: no cover
    raise SystemExit("Missing dependency: Pillow. Install with: py -m pip install Pillow") from exc

try:
    import yaml
except Exception:  # pragma: no cover
    yaml = None

import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk


def load_config(path: Optional[Path]) -> Dict[str, Any]:
    if not path:
        return {}
    with path.open("r", encoding="utf-8") as f:
        if path.suffix.lower() in {".yaml", ".yml"}:
            if yaml is None:
                raise SystemExit("PyYAML is required to read YAML config files. Install with: py -m pip install PyYAML")
            return yaml.safe_load(f) or {}
        return json.load(f)


def save_config(config: Dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        if path.suffix.lower() in {".yaml", ".yml"} and yaml is not None:
            yaml.safe_dump(config, f, sort_keys=False, allow_unicode=True)
        else:
            json.dump(config, f, indent=2, ensure_ascii=False)


def parse_page_range(page_range: str) -> List[int]:
    pages: List[int] = []
    for part in str(page_range or "").split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            start, end = int(a.strip()), int(b.strip())
            step = 1 if end >= start else -1
            pages.extend(range(start, end + step, step))
        else:
            pages.append(int(part))
    out, seen = [], set()
    for p in pages:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out


def field_options_from_config(config: Dict[str, Any]) -> List[str]:
    fields = ["sku", "page"]
    for col in config.get("optional_columns", []) or []:
        name = str(col.get("output_name", "")).strip()
        if name and name not in fields:
            fields.append(name)
    return fields


def column_config_by_field(config: Dict[str, Any], field: str) -> Dict[str, Any]:
    if field in {"sku", "page"}:
        return dict((config.get("required_columns", {}) or {}).get(field, {}) or {})
    for col in config.get("optional_columns", []) or []:
        if col.get("output_name") == field:
            return dict(col)
    return {}


def add_template_set_to_config(config: Dict[str, Any], template_path: Path, template: Dict[str, Any], apply_pdf_pages: str) -> Dict[str, Any]:
    regions = template.get("regions", []) or []
    grouped: Dict[int, List[Dict[str, Any]]] = {}
    for region in regions:
        if region.get("type") != "table_column":
            continue
        grouped.setdefault(int(region.get("block_number", 1)), []).append(region)

    coordinate_blocks: List[Dict[str, Any]] = []
    for block_no in sorted(grouped):
        regs = grouped[block_no]
        columns = {}
        for reg in regs:
            field = reg["field"]
            cfg = column_config_by_field(config, field)
            columns[field] = {
                "x0": float(reg["x0"]),
                "x1": float(reg["x1"]),
                "header_text": cfg.get("header_text", field),
                "alignment": reg.get("alignment", cfg.get("alignment", "left")),
                "inclusion_mode": reg.get("inclusion_mode", cfg.get("inclusion_mode", cfg.get("alignment", "left"))),
                "value_regex": reg.get("value_regex", cfg.get("value_regex", "") or ""),
                "header_found": False,
            }
        coordinate_blocks.append({
            "block_number": block_no,
            "block_x0": min(float(r["x0"]) for r in regs),
            "block_x1": max(float(r["x1"]) for r in regs),
            "data_top": min(float(r["y0"]) for r in regs),
            "data_bottom": max(float(r["y1"]) for r in regs),
            "columns": columns,
        })

    config.setdefault("visual_template_sets", [])
    config["visual_template_sets"].append({
        "template_path": str(template_path),
        "template_name": template.get("template_name", template_path.stem),
        "source_pdf": template.get("source_pdf", ""),
        "selection_pdf_page": template.get("selection_pdf_page", ""),
        "apply_pdf_pages": apply_pdf_pages,
        "coordinate_blocks": coordinate_blocks,
        "page_geometry": template.get("page_geometry", {}),
    })
    config["extraction_mode"] = "visual_template"
    return config


class RegionDialog(simpledialog.Dialog):
    def __init__(self, parent, fields: Sequence[str], default_block: int = 1):
        self.fields = list(fields)
        self.default_block = default_block
        self.result: Optional[Dict[str, Any]] = None
        super().__init__(parent, title="Name selected region")

    def body(self, master):
        ttk.Label(master, text="Block number:").grid(row=0, column=0, sticky="w", padx=5, pady=5)
        self.block_var = tk.StringVar(value=str(self.default_block))
        ttk.Entry(master, textvariable=self.block_var, width=10).grid(row=0, column=1, sticky="w", padx=5, pady=5)

        ttk.Label(master, text="Field:").grid(row=1, column=0, sticky="w", padx=5, pady=5)
        self.field_var = tk.StringVar(value=self.fields[0] if self.fields else "sku")
        self.field_combo = ttk.Combobox(master, textvariable=self.field_var, values=self.fields, state="readonly", width=22)
        self.field_combo.grid(row=1, column=1, sticky="w", padx=5, pady=5)

        ttk.Label(master, text="Alignment:").grid(row=2, column=0, sticky="w", padx=5, pady=5)
        self.align_var = tk.StringVar(value="left")
        ttk.Combobox(master, textvariable=self.align_var, values=["left", "right", "center", "contained", "majority"], state="readonly", width=22).grid(row=2, column=1, sticky="w", padx=5, pady=5)

        ttk.Label(master, text="Inclusion mode:").grid(row=3, column=0, sticky="w", padx=5, pady=5)
        self.inclusion_var = tk.StringVar(value="left")
        ttk.Combobox(master, textvariable=self.inclusion_var, values=["left", "right", "center", "contained", "majority", "anchor_or_overlap"], state="readonly", width=22).grid(row=3, column=1, sticky="w", padx=5, pady=5)
        return self.field_combo

    def apply(self):
        try:
            block = int(self.block_var.get().strip())
        except Exception:
            block = 1
        self.result = {
            "block_number": block,
            "field": self.field_var.get().strip(),
            "alignment": self.align_var.get().strip(),
            "inclusion_mode": self.inclusion_var.get().strip(),
        }


class RegionSelectorApp:
    def __init__(self, root: tk.Tk, pdf_path: Path, config_path: Optional[Path], page_number: int, zoom: float, output_template: Path, output_config: Optional[Path], apply_pages: str):
        self.root = root
        self.pdf_path = pdf_path
        self.config_path = config_path
        self.config = load_config(config_path)
        self.page_number = page_number
        self.zoom = zoom
        self.output_template = output_template
        self.output_config = output_config
        self.apply_pages = apply_pages or self.config.get("index_pdf_pages", str(page_number))
        self.fields = field_options_from_config(self.config) or ["sku", "page"]
        self.regions: List[Dict[str, Any]] = []
        self.rect_items: List[int] = []
        self.label_items: List[int] = []
        self.drag_start: Optional[Tuple[int, int]] = None
        self.current_rect: Optional[int] = None
        self.image_tk = None
        self.doc = fitz.open(str(pdf_path))
        self.root.title("Catalogue PDF Region Selector")
        self.build_ui()
        self.render_page()

    def build_ui(self):
        top = ttk.Frame(self.root)
        top.pack(side=tk.TOP, fill=tk.X)
        ttk.Button(top, text="Prev Page", command=self.prev_page).pack(side=tk.LEFT, padx=3, pady=3)
        ttk.Button(top, text="Next Page", command=self.next_page).pack(side=tk.LEFT, padx=3, pady=3)
        ttk.Label(top, text="Page:").pack(side=tk.LEFT, padx=3)
        self.page_var = tk.StringVar(value=str(self.page_number))
        page_entry = ttk.Entry(top, textvariable=self.page_var, width=8)
        page_entry.pack(side=tk.LEFT, padx=3)
        ttk.Button(top, text="Go", command=self.go_page).pack(side=tk.LEFT, padx=3)
        ttk.Label(top, text="Zoom:").pack(side=tk.LEFT, padx=3)
        self.zoom_var = tk.StringVar(value=str(self.zoom))
        zoom_combo = ttk.Combobox(top, textvariable=self.zoom_var, values=["1.0", "1.5", "2.0", "2.5", "3.0", "4.0"], width=6)
        zoom_combo.pack(side=tk.LEFT, padx=3)
        ttk.Button(top, text="Apply Zoom", command=self.apply_zoom).pack(side=tk.LEFT, padx=3)
        ttk.Button(top, text="Delete Last", command=self.delete_last).pack(side=tk.LEFT, padx=10)
        ttk.Button(top, text="Clear", command=self.clear_regions).pack(side=tk.LEFT, padx=3)
        ttk.Button(top, text="Save Template", command=self.save_template).pack(side=tk.LEFT, padx=10)
        ttk.Button(top, text="Save + Close", command=lambda: (self.save_template(), self.root.destroy())).pack(side=tk.LEFT, padx=3)

        main = ttk.Frame(self.root)
        main.pack(fill=tk.BOTH, expand=True)
        self.canvas = tk.Canvas(main, bg="grey")
        self.hbar = ttk.Scrollbar(main, orient=tk.HORIZONTAL, command=self.canvas.xview)
        self.vbar = ttk.Scrollbar(main, orient=tk.VERTICAL, command=self.canvas.yview)
        self.canvas.configure(xscrollcommand=self.hbar.set, yscrollcommand=self.vbar.set)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.vbar.grid(row=0, column=1, sticky="ns")
        self.hbar.grid(row=1, column=0, sticky="ew")
        main.rowconfigure(0, weight=1)
        main.columnconfigure(0, weight=1)
        self.canvas.bind("<ButtonPress-1>", self.on_press)
        self.canvas.bind("<B1-Motion>", self.on_drag)
        self.canvas.bind("<ButtonRelease-1>", self.on_release)

        bottom = ttk.Frame(self.root)
        bottom.pack(side=tk.BOTTOM, fill=tk.X)
        self.status_var = tk.StringVar(value="Drag on the page to select a table column region. Include the column header and data rows.")
        ttk.Label(bottom, textvariable=self.status_var).pack(side=tk.LEFT, padx=5, pady=3)

    def render_page(self):
        if self.page_number < 1:
            self.page_number = 1
        if self.page_number > len(self.doc):
            self.page_number = len(self.doc)
        page = self.doc[self.page_number - 1]
        matrix = fitz.Matrix(self.zoom, self.zoom)
        pix = page.get_pixmap(matrix=matrix, alpha=False)
        img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
        self.image_tk = ImageTk.PhotoImage(img)
        self.canvas.delete("all")
        self.rect_items.clear()
        self.label_items.clear()
        self.canvas.create_image(0, 0, anchor="nw", image=self.image_tk)
        self.canvas.configure(scrollregion=(0, 0, img.width, img.height))
        self.page_var.set(str(self.page_number))
        self.redraw_regions()
        self.status_var.set(f"PDF page {self.page_number} / {len(self.doc)} at {self.zoom}x. Drag to select columns.")

    def image_to_pdf(self, x: float, y: float) -> Tuple[float, float]:
        return x / self.zoom, y / self.zoom

    def pdf_to_image(self, x: float, y: float) -> Tuple[float, float]:
        return x * self.zoom, y * self.zoom

    def on_press(self, event):
        x = int(self.canvas.canvasx(event.x))
        y = int(self.canvas.canvasy(event.y))
        self.drag_start = (x, y)
        self.current_rect = self.canvas.create_rectangle(x, y, x, y, outline="red", width=2)

    def on_drag(self, event):
        if self.drag_start and self.current_rect:
            x0, y0 = self.drag_start
            x1 = int(self.canvas.canvasx(event.x))
            y1 = int(self.canvas.canvasy(event.y))
            self.canvas.coords(self.current_rect, x0, y0, x1, y1)

    def on_release(self, event):
        if not self.drag_start or not self.current_rect:
            return
        x0, y0 = self.drag_start
        x1 = int(self.canvas.canvasx(event.x))
        y1 = int(self.canvas.canvasy(event.y))
        ix0, ix1 = sorted([x0, x1])
        iy0, iy1 = sorted([y0, y1])
        if abs(ix1 - ix0) < 5 or abs(iy1 - iy0) < 5:
            self.canvas.delete(self.current_rect)
            self.current_rect = None
            self.drag_start = None
            return
        default_block = 1
        if self.regions:
            default_block = int(self.regions[-1].get("block_number", 1))
        dialog = RegionDialog(self.root, self.fields, default_block=default_block)
        if not dialog.result:
            self.canvas.delete(self.current_rect)
            self.current_rect = None
            self.drag_start = None
            return
        px0, py0 = self.image_to_pdf(ix0, iy0)
        px1, py1 = self.image_to_pdf(ix1, iy1)
        page = self.doc[self.page_number - 1]
        region = {
            "field": dialog.result["field"],
            "type": "table_column",
            "block_number": dialog.result["block_number"],
            "page": self.page_number,
            "x0": round(px0, 3),
            "y0": round(py0, 3),
            "x1": round(px1, 3),
            "y1": round(py1, 3),
            "alignment": dialog.result["alignment"],
            "inclusion_mode": dialog.result["inclusion_mode"],
            "page_width": round(float(page.rect.width), 3),
            "page_height": round(float(page.rect.height), 3),
            "rotation": int(page.rotation),
            "cropbox": [round(float(v), 3) for v in page.cropbox],
            "mediabox": [round(float(v), 3) for v in page.mediabox],
            "render_zoom_x": self.zoom,
            "render_zoom_y": self.zoom,
        }
        self.regions.append(region)
        self.canvas.delete(self.current_rect)
        self.current_rect = None
        self.drag_start = None
        self.redraw_regions()

    def redraw_regions(self):
        for item in self.rect_items + self.label_items:
            self.canvas.delete(item)
        self.rect_items.clear(); self.label_items.clear()
        colors = {"sku": "#d7191c", "page": "#2c7bb6", "pack_carton": "#fdae61", "pallet": "#1a9641"}
        for idx, reg in enumerate(self.regions, start=1):
            if int(reg.get("page", self.page_number)) != self.page_number:
                continue
            x0, y0 = self.pdf_to_image(float(reg["x0"]), float(reg["y0"]))
            x1, y1 = self.pdf_to_image(float(reg["x1"]), float(reg["y1"]))
            color = colors.get(reg.get("field"), "purple")
            rect = self.canvas.create_rectangle(x0, y0, x1, y1, outline=color, width=2)
            label = self.canvas.create_text(x0 + 4, y0 + 4, anchor="nw", text=f"B{reg.get('block_number')} {reg.get('field')}", fill=color)
            self.rect_items.append(rect); self.label_items.append(label)

    def save_template(self):
        page = self.doc[self.page_number - 1]
        template = {
            "template_name": self.output_template.stem,
            "created_with": "catalogue_region_selector.py",
            "version": "1.0",
            "template_type": "catalogue_index_visual_template",
            "source_pdf": str(self.pdf_path),
            "selection_pdf_page": self.page_number,
            "apply_pdf_pages": self.apply_pages,
            "page_geometry": {
                "page_width": round(float(page.rect.width), 3),
                "page_height": round(float(page.rect.height), 3),
                "rotation": int(page.rotation),
                "cropbox": [round(float(v), 3) for v in page.cropbox],
                "mediabox": [round(float(v), 3) for v in page.mediabox],
            },
            "regions": self.regions,
        }
        required_by_block: Dict[int, set] = {}
        for reg in self.regions:
            required_by_block.setdefault(int(reg.get("block_number", 1)), set()).add(str(reg.get("field")))
        bad_blocks = []
        for block, fields in sorted(required_by_block.items()):
            missing = {"sku", "page"} - fields
            if missing:
                bad_blocks.append(f"block {block}: missing {', '.join(sorted(missing))}")
        if bad_blocks:
            if not messagebox.askyesno("Required regions missing", "Some blocks are missing sku/page regions:\n" + "\n".join(bad_blocks) + "\n\nSave anyway?"):
                return
        self.output_template.parent.mkdir(parents=True, exist_ok=True)
        with self.output_template.open("w", encoding="utf-8") as f:
            json.dump(template, f, indent=2, ensure_ascii=False)
        if self.output_config and self.config:
            config_copy = dict(self.config)
            add_template_set_to_config(config_copy, self.output_template, template, self.apply_pages)
            save_config(config_copy, self.output_config)
        messagebox.showinfo("Saved", f"Template saved:\n{self.output_template}" + (f"\n\nConfig saved:\n{self.output_config}" if self.output_config else ""))

    def delete_last(self):
        if self.regions:
            self.regions.pop()
            self.redraw_regions()

    def clear_regions(self):
        if messagebox.askyesno("Clear regions", "Delete all selected regions?"):
            self.regions.clear()
            self.redraw_regions()

    def prev_page(self):
        if self.page_number > 1:
            self.page_number -= 1
            self.render_page()

    def next_page(self):
        if self.page_number < len(self.doc):
            self.page_number += 1
            self.render_page()

    def go_page(self):
        try:
            self.page_number = int(self.page_var.get())
        except Exception:
            pass
        self.render_page()

    def apply_zoom(self):
        try:
            self.zoom = float(self.zoom_var.get())
        except Exception:
            self.zoom = 2.0
        self.render_page()


def parse_args(argv: Optional[Sequence[str]] = None):
    parser = argparse.ArgumentParser(description="Drag-select PDF table column regions and save a visual extraction template.")
    parser.add_argument("--pdf", required=True, help="Input PDF path.")
    parser.add_argument("--config", help="Existing extractor YAML/JSON config used to populate field dropdowns.")
    parser.add_argument("--page", type=int, default=1, help="1-based PDF page to open for selection.")
    parser.add_argument("--zoom", type=float, default=2.0, help="Render zoom. Image is displayed at actual rendered size.")
    parser.add_argument("--output-template", required=True, help="Path to save visual template JSON.")
    parser.add_argument("--output-config", help="Optional path to save a config with this visual template attached.")
    parser.add_argument("--apply-pages", help="PDF page range the template should apply to. Defaults to config index_pdf_pages or selected page.")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> None:
    args = parse_args(argv)
    root = tk.Tk()
    app = RegionSelectorApp(
        root=root,
        pdf_path=Path(args.pdf),
        config_path=Path(args.config) if args.config else None,
        page_number=args.page,
        zoom=args.zoom,
        output_template=Path(args.output_template),
        output_config=Path(args.output_config) if args.output_config else None,
        apply_pages=args.apply_pages or "",
    )
    root.geometry("1200x850")
    root.mainloop()


if __name__ == "__main__":
    main()
