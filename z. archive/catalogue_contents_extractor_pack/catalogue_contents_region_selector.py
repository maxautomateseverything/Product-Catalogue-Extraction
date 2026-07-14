#!/usr/bin/env python3
"""
Catalogue Contents Region Selector
----------------------------------
Visual helper for creating extraction regions for catalogue contents pages.

The rendered image is only used for selection. Saved coordinates are PDF-native
coordinates and are later applied to the original selectable-text PDF.

Usage:
  py catalogue_contents_region_selector.py --pdf catalogue.pdf --page 4 --output-template contents_regions.json
  py catalogue_contents_region_selector.py --pdf catalogue.pdf --config contents_config.yaml --page 4 --output-template regions.json --output-config contents_config_with_template.yaml
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

try:
    import fitz
except ImportError as exc:  # pragma: no cover
    raise SystemExit("Missing dependency: PyMuPDF. Install with: py -m pip install PyMuPDF") from exc

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

try:
    import tkinter as tk
    from tkinter import simpledialog, messagebox, filedialog, ttk
    from PIL import Image, ImageTk
except ImportError as exc:  # pragma: no cover
    raise SystemExit("Missing GUI dependency. tkinter and Pillow are required for the selector.") from exc

REGION_TYPES = [
    "content_region",
    "ignore_region",
    "table_column",
    "card_page",
    "card_title",
    "card_region",
    "header_region",
]

FIELD_SUGGESTIONS = [
    "main_header",
    "subheader_1",
    "subheader_2",
    "subheader_3",
    "page_number",
    "subheader_2_page_number_combined",
]


def log(message: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


def load_config(path: Optional[Path]) -> Dict[str, Any]:
    if not path:
        return {}
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


class RegionDialog(simpledialog.Dialog):
    def __init__(self, parent: tk.Tk, defaults: Dict[str, Any]):
        self.defaults = defaults
        self.result_data: Optional[Dict[str, Any]] = None
        super().__init__(parent, title="Region details")

    def body(self, master: tk.Widget) -> tk.Widget:
        tk.Label(master, text="Region type").grid(row=0, column=0, sticky="w")
        self.region_type = ttk.Combobox(master, values=REGION_TYPES, state="readonly")
        self.region_type.set(self.defaults.get("type", "content_region"))
        self.region_type.grid(row=0, column=1, sticky="ew")

        tk.Label(master, text="Name").grid(row=1, column=0, sticky="w")
        self.name = tk.Entry(master)
        self.name.insert(0, self.defaults.get("name", ""))
        self.name.grid(row=1, column=1, sticky="ew")

        tk.Label(master, text="Field/output column").grid(row=2, column=0, sticky="w")
        self.field = ttk.Combobox(master, values=FIELD_SUGGESTIONS)
        self.field.set(self.defaults.get("field", ""))
        self.field.grid(row=2, column=1, sticky="ew")

        tk.Label(master, text="Group name").grid(row=3, column=0, sticky="w")
        self.group = tk.Entry(master)
        self.group.insert(0, self.defaults.get("group", ""))
        self.group.grid(row=3, column=1, sticky="ew")

        tk.Label(master, text="Reading order").grid(row=4, column=0, sticky="w")
        self.reading_order = tk.Entry(master)
        self.reading_order.insert(0, str(self.defaults.get("reading_order", "1")))
        self.reading_order.grid(row=4, column=1, sticky="ew")

        tk.Label(master, text="Page range this applies to").grid(row=5, column=0, sticky="w")
        self.page_range = tk.Entry(master)
        self.page_range.insert(0, str(self.defaults.get("page_range", "")))
        self.page_range.grid(row=5, column=1, sticky="ew")

        tk.Label(master, text="Fixed context JSON").grid(row=6, column=0, sticky="nw")
        self.fixed_context = tk.Text(master, height=5, width=50)
        fc = self.defaults.get("fixed_context", {})
        self.fixed_context.insert("1.0", json.dumps(fc, indent=2) if isinstance(fc, dict) else str(fc))
        self.fixed_context.grid(row=6, column=1, sticky="ew")

        tk.Label(master, text="Notes").grid(row=7, column=0, sticky="w")
        self.notes = tk.Entry(master)
        self.notes.insert(0, self.defaults.get("notes", ""))
        self.notes.grid(row=7, column=1, sticky="ew")
        master.columnconfigure(1, weight=1)
        return self.name

    def apply(self) -> None:
        try:
            fc_text = self.fixed_context.get("1.0", "end").strip()
            fixed_context = json.loads(fc_text) if fc_text else {}
            if not isinstance(fixed_context, dict):
                raise ValueError("Fixed context must be a JSON object.")
        except Exception as exc:
            messagebox.showerror("Invalid fixed context", f"Fixed context must be valid JSON, for example {{\"main_header\": \"COMMERCIAL\"}}.\n\n{exc}")
            self.result_data = None
            return
        self.result_data = {
            "type": self.region_type.get(),
            "name": self.name.get().strip(),
            "field": self.field.get().strip(),
            "group": self.group.get().strip(),
            "reading_order": int(self.reading_order.get().strip() or "1"),
            "page_range": self.page_range.get().strip(),
            "fixed_context": fixed_context,
            "notes": self.notes.get().strip(),
        }


class SelectorApp:
    def __init__(self, root: tk.Tk, pdf_path: Path, page_number: int, zoom: float, output_template: Path, config_path: Optional[Path] = None, output_config: Optional[Path] = None, apply_pages: str = ""):
        self.root = root
        self.pdf_path = pdf_path
        self.page_number = page_number
        self.zoom = zoom
        self.output_template = output_template
        self.config_path = config_path
        self.output_config = output_config
        self.apply_pages = apply_pages
        self.config = load_config(config_path)
        self.doc = fitz.open(str(pdf_path))
        if page_number < 1 or page_number > self.doc.page_count:
            raise SystemExit(f"Page {page_number} is out of range for PDF with {self.doc.page_count} pages.")
        self.page = self.doc[page_number - 1]
        self.regions: List[Dict[str, Any]] = []
        self.start_x = self.start_y = 0
        self.current_rect_id = None
        self.tk_img = None

        self.root.title("Catalogue Contents Region Selector")
        self.build_ui()
        self.render_page()

    def build_ui(self) -> None:
        top = tk.Frame(self.root)
        top.pack(side=tk.TOP, fill=tk.X)
        tk.Button(top, text="Save template", command=self.save_template).pack(side=tk.LEFT, padx=4, pady=4)
        tk.Button(top, text="Load template", command=self.load_template_dialog).pack(side=tk.LEFT, padx=4, pady=4)
        tk.Button(top, text="Delete selected", command=self.delete_selected).pack(side=tk.LEFT, padx=4, pady=4)
        tk.Button(top, text="Clear all", command=self.clear_all).pack(side=tk.LEFT, padx=4, pady=4)
        tk.Button(top, text="Quit", command=self.root.destroy).pack(side=tk.RIGHT, padx=4, pady=4)
        self.status = tk.Label(top, text="Drag a rectangle on the PDF page to create a region.")
        self.status.pack(side=tk.LEFT, padx=10)

        main = tk.Frame(self.root)
        main.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        self.canvas = tk.Canvas(main, bg="white", cursor="crosshair")
        hbar = tk.Scrollbar(main, orient=tk.HORIZONTAL, command=self.canvas.xview)
        vbar = tk.Scrollbar(main, orient=tk.VERTICAL, command=self.canvas.yview)
        self.canvas.configure(xscrollcommand=hbar.set, yscrollcommand=vbar.set)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        vbar.grid(row=0, column=1, sticky="ns")
        hbar.grid(row=1, column=0, sticky="ew")
        main.rowconfigure(0, weight=1)
        main.columnconfigure(0, weight=1)

        side = tk.Frame(main, width=320)
        side.grid(row=0, column=2, sticky="ns")
        tk.Label(side, text="Selected regions").pack(anchor="w")
        self.listbox = tk.Listbox(side, width=55, height=35)
        self.listbox.pack(fill=tk.BOTH, expand=True)

        self.canvas.bind("<ButtonPress-1>", self.on_mouse_down)
        self.canvas.bind("<B1-Motion>", self.on_mouse_drag)
        self.canvas.bind("<ButtonRelease-1>", self.on_mouse_up)

    def render_page(self) -> None:
        pix = self.page.get_pixmap(matrix=fitz.Matrix(self.zoom, self.zoom), alpha=False)
        mode = "RGB"
        img = Image.frombytes(mode, [pix.width, pix.height], pix.samples)
        self.tk_img = ImageTk.PhotoImage(img)
        self.canvas.create_image(0, 0, image=self.tk_img, anchor="nw", tags="page")
        self.canvas.config(scrollregion=(0, 0, pix.width, pix.height))

    def pdf_to_canvas(self, x: float, y: float) -> Tuple[float, float]:
        return x * self.zoom, y * self.zoom

    def canvas_to_pdf(self, x: float, y: float) -> Tuple[float, float]:
        return x / self.zoom, y / self.zoom

    def on_mouse_down(self, event: tk.Event) -> None:
        self.start_x = self.canvas.canvasx(event.x)
        self.start_y = self.canvas.canvasy(event.y)
        if self.current_rect_id:
            self.canvas.delete(self.current_rect_id)
        self.current_rect_id = self.canvas.create_rectangle(self.start_x, self.start_y, self.start_x, self.start_y, outline="red", width=2, dash=(4, 2))

    def on_mouse_drag(self, event: tk.Event) -> None:
        x = self.canvas.canvasx(event.x)
        y = self.canvas.canvasy(event.y)
        if self.current_rect_id:
            self.canvas.coords(self.current_rect_id, self.start_x, self.start_y, x, y)

    def on_mouse_up(self, event: tk.Event) -> None:
        x = self.canvas.canvasx(event.x)
        y = self.canvas.canvasy(event.y)
        x0, x1 = sorted([self.start_x, x])
        y0, y1 = sorted([self.start_y, y])
        if abs(x1 - x0) < 5 or abs(y1 - y0) < 5:
            if self.current_rect_id:
                self.canvas.delete(self.current_rect_id)
            return
        defaults = {"page_range": self.apply_pages or str(self.page_number)}
        dlg = RegionDialog(self.root, defaults)
        data = dlg.result_data
        if not data:
            if self.current_rect_id:
                self.canvas.delete(self.current_rect_id)
            return
        pdf_x0, pdf_y0 = self.canvas_to_pdf(x0, y0)
        pdf_x1, pdf_y1 = self.canvas_to_pdf(x1, y1)
        region = {
            **data,
            "page": self.page_number,
            "x0": round(pdf_x0, 3),
            "y0": round(pdf_y0, 3),
            "x1": round(pdf_x1, 3),
            "y1": round(pdf_y1, 3),
            "page_width": round(float(self.page.rect.width), 3),
            "page_height": round(float(self.page.rect.height), 3),
            "rotation": self.page.rotation,
            "cropbox": [round(float(v), 3) for v in self.page.cropbox],
            "mediabox": [round(float(v), 3) for v in self.page.mediabox],
            "render_zoom": self.zoom,
        }
        self.regions.append(region)
        if self.current_rect_id:
            self.canvas.delete(self.current_rect_id)
            self.current_rect_id = None
        self.draw_region(region)
        self.refresh_listbox()

    def draw_region(self, region: Dict[str, Any]) -> None:
        x0, y0 = self.pdf_to_canvas(region["x0"], region["y0"])
        x1, y1 = self.pdf_to_canvas(region["x1"], region["y1"])
        color = {
            "content_region": "blue",
            "ignore_region": "red",
            "table_column": "green",
            "card_page": "purple",
            "card_title": "orange",
            "card_region": "brown",
            "header_region": "darkcyan",
        }.get(region.get("type"), "black")
        tag = f"region_{len(self.regions)}"
        self.canvas.create_rectangle(x0, y0, x1, y1, outline=color, width=2, tags=("region", tag))
        label = f"{region.get('type')}:{region.get('group') or region.get('name')}:{region.get('field')}"
        self.canvas.create_text(x0 + 2, max(8, y0 - 8), text=label, anchor="w", fill=color, tags=("region", tag))

    def refresh_canvas_regions(self) -> None:
        self.canvas.delete("region")
        for r in self.regions:
            self.draw_region(r)

    def refresh_listbox(self) -> None:
        self.listbox.delete(0, tk.END)
        for idx, r in enumerate(self.regions, start=1):
            self.listbox.insert(tk.END, f"{idx}. {r.get('type')} | group={r.get('group')} | field={r.get('field')} | page_range={r.get('page_range')} | x={r.get('x0')}-{r.get('x1')} y={r.get('y0')}-{r.get('y1')}")

    def delete_selected(self) -> None:
        sel = self.listbox.curselection()
        if not sel:
            return
        idx = int(sel[0])
        if 0 <= idx < len(self.regions):
            self.regions.pop(idx)
        self.refresh_canvas_regions()
        self.refresh_listbox()

    def clear_all(self) -> None:
        if messagebox.askyesno("Clear all regions", "Remove all selected regions?"):
            self.regions.clear()
            self.refresh_canvas_regions()
            self.refresh_listbox()

    def build_template(self) -> Dict[str, Any]:
        return {
            "template_name": self.output_template.stem,
            "created_with": "catalogue_contents_region_selector",
            "version": "1.0",
            "pdf_reference": {
                "file_name": self.pdf_path.name,
                "page_count": self.doc.page_count,
                "default_page": self.page_number,
                "page_width": round(float(self.page.rect.width), 3),
                "page_height": round(float(self.page.rect.height), 3),
                "rotation": self.page.rotation,
            },
            "apply_pdf_pages": self.apply_pages,
            "regions": self.regions,
        }

    def save_template(self) -> None:
        self.output_template.parent.mkdir(parents=True, exist_ok=True)
        with self.output_template.open("w", encoding="utf-8") as f:
            json.dump(self.build_template(), f, indent=2, ensure_ascii=False)
        self.status.config(text=f"Saved template: {self.output_template}")
        log(f"Saved template: {self.output_template}")
        if self.output_config:
            config = dict(self.config)
            templates = config.setdefault("visual_templates", [])
            templates.append({"template_path": str(self.output_template), "apply_pdf_pages": self.apply_pages or str(self.page_number)})
            save_config(config, self.output_config)
            log(f"Saved config with template reference: {self.output_config}")
            self.status.config(text=f"Saved template and config: {self.output_config}")

    def load_template_dialog(self) -> None:
        path = filedialog.askopenfilename(title="Load visual template", filetypes=[("JSON", "*.json"), ("All files", "*.*")])
        if not path:
            return
        with open(path, "r", encoding="utf-8") as f:
            template = json.load(f)
        self.regions = template.get("regions", []) or []
        self.refresh_canvas_regions()
        self.refresh_listbox()
        self.status.config(text=f"Loaded template: {path}")


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Draw PDF-native regions for catalogue contents extraction.")
    parser.add_argument("--pdf", required=True, help="PDF path.")
    parser.add_argument("--config", help="Optional existing config to attach template reference to.")
    parser.add_argument("--page", type=int, required=True, help="1-based PDF page number to show.")
    parser.add_argument("--zoom", type=float, default=2.0, help="Render zoom. Image is displayed at actual rendered size.")
    parser.add_argument("--output-template", required=True, help="Output JSON template path.")
    parser.add_argument("--output-config", help="Optional output config path with this template reference appended.")
    parser.add_argument("--apply-pages", default="", help="Page range the template/regions should apply to, e.g. 4-6 or 4,6,8.")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> None:
    args = parse_args(argv)
    root = tk.Tk()
    app = SelectorApp(
        root=root,
        pdf_path=Path(args.pdf),
        page_number=args.page,
        zoom=args.zoom,
        output_template=Path(args.output_template),
        config_path=Path(args.config) if args.config else None,
        output_config=Path(args.output_config) if args.output_config else None,
        apply_pages=args.apply_pages,
    )
    root.mainloop()


if __name__ == "__main__":
    main()
