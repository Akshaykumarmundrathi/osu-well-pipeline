"""
inspect_pdfs.py  —  Manual PDF inspection viewer for OSU pipeline calibration
==============================================================================

Opens each sampled PDF one at a time and lets you draw 4 named rectangles
in order to capture the position and size of each key field:

  Draw 1 → GRID       (amber)
  Draw 2 → COUNTY     (green)
  Draw 3 → STR        (blue  — section/township/range box)
  Draw 4 → LAT/LON    (pink  — skip if not present on this file)

All four rectangles stay visible simultaneously. Saves pixel coords AND
page-fraction coords so comparisons across different scan resolutions work.

Usage:
    python tools/inspect_pdfs.py --folder D:/inspection_pdfs
    python tools/inspect_pdfs.py --folder D:/inspection_pdfs --collections 1 7
    python tools/inspect_pdfs.py --resume      # skip already-noted files

Keyboard shortcuts:
    -> / D / Space   Next PDF
    <-  / A          Previous PDF
    Up  / W          Previous page
    Down / S         Next page
    Enter            Save note + Next PDF
    X                Clear the active slot's rectangle
    T                Open OCR text popup
    Esc              Quit

Slot selection:
    Click any slot chip (1-GRID, 2-COUNTY, 3-STR, 4-LAT/LON) to make it
    the active target for the next drag.  Slots are fully independent —
    draw LAT/LON first if you like, skip GRID entirely, etc.
"""

import argparse
import csv
import os
import sys
import threading
import tkinter as tk
from tkinter import ttk, messagebox
from pathlib import Path
from datetime import datetime

try:
    import fitz
except ImportError:
    sys.exit("ERROR: PyMuPDF not installed.  pip install pymupdf")

try:
    from PIL import Image, ImageTk
except ImportError:
    sys.exit("ERROR: Pillow not installed.  pip install Pillow")

try:
    import pytesseract
    _TESS_OK = True
except ImportError:
    _TESS_OK = False

import re as _re

# ── Slot definitions: (key, label, rect_color, text_color) ───────────────────
SLOT_DEFS = [
    ("grid",   "1-GRID",    "#f59e0b", "#d97706"),
    ("county", "2-COUNTY",  "#4ade80", "#16a34a"),
    ("str",    "3-STR",     "#7dd3fc", "#0284c7"),
    ("latlon", "4-LAT/LON", "#f9a8d4", "#db2777"),
]

# CSV fields saved per slot (32 total = 4 slots * 8 dimensions)
SEL_FIELDS = []
for _sn, *_ in SLOT_DEFS:
    for _d in ["x_px", "y_px", "w_px", "h_px", "x_pct", "y_pct", "w_pct", "h_pct"]:
        SEL_FIELDS.append(f"{_sn}_{_d}")

# ── Pattern definitions for auto-detection ────────────────────────────────────
_PAT = {
    "county":   _re.compile(
                    r'\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)\s+[Cc]ounty\b'
                    r'|[Cc]ounty[\s:,]+([A-Z][A-Za-z ]+?)(?:\n|,|\d)',
                    _re.MULTILINE),
    "section":  _re.compile(r'[Ss]ec(?:tion)?\.?\s*#?\s*(\d{1,2})\b'),
    "township": _re.compile(r'[Tt](?:wp?|ownship)?\.?\s*(\d{1,3})\s*([NnSs])\b'),
    "range":    _re.compile(r'[Rr](?:ge?|ange)?\.?\s*(\d{1,3})\s*([EeWw])\b'),
    "dms":      _re.compile(r'(\d{2,3})\s*[°o\*]\s*(\d{1,2})\s*[\'`]\s*(\d{0,2})'),
    "lat_dec":  _re.compile(r'\b(3[4-7]\.\d{3,6})\b'),
    "lon_dec":  _re.compile(r'\b(9[4-9]\.\d{3,6}|10[0-3]\.\d{3,6})\b'),
    "lat_lbl":  _re.compile(r'[Ll]at(?:itude)?[\s:.]+([0-9\'".\- ]{4,25})'),
    "lon_lbl":  _re.compile(r'[Ll]on(?:g(?:itude)?)?[\s:.]+([0-9\'".\- ]{4,25})'),
    "dot_nw":   _re.compile(r'\b(NW|NE|SW|SE)\s*(?:1/4|quarter|corner|of)', _re.I),
    "quadrant": _re.compile(r'\b(NW|NE|SW|SE)\b'),
}

# ── Tier descriptions ─────────────────────────────────────────────────────────
TIER_INFO = {
    1:  ("early",      "~1911-1940s",  "Handwritten STR, 8x8 grid, no lat/lon"),
    2:  ("early",      "~1911-1940s",  "Handwritten STR, 8x8 grid, no lat/lon"),
    3:  ("early",      "~1911-1940s",  "Handwritten STR, 8x8 grid, no lat/lon"),
    4:  ("early",      "~1911-1940s",  "Handwritten STR, 8x8 grid, no lat/lon"),
    5:  ("early",      "~1911-1940s",  "Handwritten STR, 8x8 grid, no lat/lon"),
    6:  ("early",      "~1911-1940s",  "Handwritten STR, 8x8 grid, no lat/lon"),
    7:  ("transition", "~1950s",       "Mixed layout, STR present, grid position shifts"),
    8:  ("transition", "~1950s",       "Mixed layout, STR present, grid position shifts"),
    9:  ("mid",        "~1960s-70s",   "'Location:' line dominant, STR varies"),
    10: ("mid",        "~1960s-70s",   "'Location:' line dominant, STR varies"),
    11: ("late",       "~1980s-90s",   "Lat/lon printed on form + Location line"),
    12: ("late",       "~1980s-90s",   "Lat/lon printed on form + Location line"),
    13: ("modern",     "~2000s-2024",  "Decimal degrees, digital forms"),
}


# ── Load manifest ─────────────────────────────────────────────────────────────

def load_manifest(folder: Path, only_collections: list) -> list:
    idx = folder / "_inspection_index.csv"
    if not idx.exists():
        sys.exit(f"ERROR: No _inspection_index.csv in {folder}")
    entries = []
    with open(idx, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row.get("status") != "ok":
                continue
            if not Path(row["local_path"]).exists():
                continue
            if only_collections:
                m = _re.search(r"\d+", row.get("collection", ""))
                if not m or int(m.group()) not in only_collections:
                    continue
            entries.append(row)
    return entries


def load_existing_notes(notes_path: Path) -> dict:
    if not notes_path.exists():
        return {}
    out = {}
    with open(notes_path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            out[row.get("local_path", "")] = row
    return out


# ── PDF helpers ───────────────────────────────────────────────────────────────

def render_page(pdf_path: str, page_num: int, max_width: int = 900):
    """Returns (PhotoImage, (iw, ih), scale)."""
    doc   = fitz.open(pdf_path)
    page  = doc[page_num]
    scale = min(max_width / page.rect.width, 2.0)
    mat   = fitz.Matrix(scale, scale)
    pix   = page.get_pixmap(matrix=mat, colorspace=fitz.csGRAY, alpha=False)
    img   = Image.frombytes("L", [pix.width, pix.height], pix.samples).convert("RGB")
    doc.close()
    return ImageTk.PhotoImage(img), img.size, scale


def page_count(pdf_path: str) -> int:
    doc = fitz.open(pdf_path)
    n   = doc.page_count
    doc.close()
    return n


def _highlight_text(widget, keyword: str, tag: str):
    start = "1.0"
    while True:
        pos = widget.search(keyword, start, stopindex="end", nocase=True)
        if not pos:
            break
        end = f"{pos}+{len(keyword)}c"
        widget.tag_add(tag, pos, end)
        start = end


def ocr_page(pdf_path: str, page_num: int, max_width: int = 1400) -> tuple:
    """Returns (text, method).  Tries embedded text first, then Tesseract."""
    doc  = fitz.open(pdf_path)
    page = doc[page_num]
    embedded = page.get_text("text").strip()
    if len(embedded) > 40:
        doc.close()
        return embedded, "embedded"
    scale = min(max_width / page.rect.width, 2.5)
    pix   = page.get_pixmap(matrix=fitz.Matrix(scale, scale),
                             colorspace=fitz.csGRAY, alpha=False)
    img   = Image.frombytes("L", [pix.width, pix.height], pix.samples)
    doc.close()
    if not _TESS_OK:
        return "(pytesseract not installed)", "none"
    try:
        return pytesseract.image_to_string(img, config="--psm 6").strip(), "tesseract"
    except Exception as exc:
        return f"(OCR error: {exc})", "none"


def extract_patterns(text: str) -> dict:
    out = {}
    m = _PAT["county"].search(text)
    if m:
        out["county"] = (m.group(1) or m.group(2) or "").strip()
    m = _PAT["section"].search(text)
    if m:
        out["section"] = m.group(1)
    m = _PAT["township"].search(text)
    if m:
        out["township"] = f"{m.group(1)}{m.group(2).upper()}"
    m = _PAT["range"].search(text)
    if m:
        out["range"] = f"{m.group(1)}{m.group(2).upper()}"
    lat_dec = _PAT["lat_dec"].findall(text)
    lon_dec = _PAT["lon_dec"].findall(text)
    dms_all = _PAT["dms"].findall(text)
    lat_lbl = _PAT["lat_lbl"].findall(text)
    lon_lbl = _PAT["lon_lbl"].findall(text)
    if lat_dec or lon_dec:
        out["latlon_format"] = "decimal"
        if lat_dec: out["lat"] = lat_dec[0]
        if lon_dec: out["lon"] = lon_dec[0]
    elif dms_all:
        out["latlon_format"] = "DMS"
        out["dms_samples"] = [f"{d}deg{mn}'{s}\"" for d,mn,s in dms_all[:2]]
    elif lat_lbl or lon_lbl:
        out["latlon_format"] = "labeled"
        if lat_lbl: out["lat_raw"] = lat_lbl[0].strip()
        if lon_lbl: out["lon_raw"] = lon_lbl[0].strip()
    else:
        out["latlon_format"] = "not found"
    dot_nw = _PAT["dot_nw"].findall(text)
    if dot_nw:
        out["dot_nw"] = dot_nw[0].upper()
    else:
        quads = list(dict.fromkeys(_PAT["quadrant"].findall(text)))
        if quads:
            out["quadrants_seen"] = " ".join(quads)
    return out


# ── Main viewer app ───────────────────────────────────────────────────────────

class InspectionApp:
    def __init__(self, root: tk.Tk, entries: list, notes_path: Path, resume: bool):
        self.root       = root
        self.entries    = entries
        self.notes_path = notes_path
        self.notes      = load_existing_notes(notes_path)
        self.idx        = 0
        self.page_idx   = 0

        # ── Multi-slot selection state ────────────────────────────────────────
        self._slots       = {}    # key → {x_px,y_px,w_px,h_px,x_pct,y_pct,w_pct,h_pct}
        self._slot_rects  = {}    # key → canvas rect item ID
        self._slot_texts  = {}    # key → canvas text item ID
        self._active_slot = 0     # index into SLOT_DEFS for next draw
        self._sel_start   = None  # (canvas_x, canvas_y) drag start
        self._live_rect   = None  # live-drag temporary rect ID
        self._live_text   = None  # live-drag dimension hint ID
        self._img_offset  = (10, 10)
        self._img_size    = (900, 1200)

        if resume:
            noted = set(self.notes.keys())
            for i, e in enumerate(entries):
                if e["local_path"] not in noted:
                    self.idx = i
                    break

        root.title("OSU Pipeline — PDF Inspector")
        root.configure(bg="#0f172a")
        root.geometry("1140x860")
        root.minsize(800, 600)
        self._build_ui()
        self._bind_keys()
        self._load_current()

    # ── UI construction ───────────────────────────────────────────────────────

    def _build_ui(self):
        BG, BG2, FG, ACC = "#0f172a", "#1e293b", "#e2e8f0", "#f59e0b"

        # ── Top bar ───────────────────────────────────────────────────────────
        top = tk.Frame(self.root, bg="#0d2d52", pady=6, padx=12)
        top.pack(fill="x")
        self.lbl_progress = tk.Label(top, text="", bg="#0d2d52", fg=ACC,
                                     font=("Segoe UI", 11, "bold"))
        self.lbl_progress.pack(side="left")
        self.lbl_context = tk.Label(top, text="", bg="#0d2d52", fg="#90bce0",
                                    font=("Segoe UI", 10))
        self.lbl_context.pack(side="left", padx=16)
        self.pbar_var = tk.DoubleVar()
        ttk.Progressbar(top, variable=self.pbar_var,
                        maximum=len(self.entries), length=200).pack(side="right", padx=8)

        # ── Tier banner ───────────────────────────────────────────────────────
        tier_bar = tk.Frame(self.root, bg="#1a1a2e", pady=3, padx=12)
        tier_bar.pack(fill="x")
        self.lbl_tier = tk.Label(tier_bar, text="", bg="#1a1a2e",
                                 fg="#60a5fa", font=("Segoe UI", 9))
        self.lbl_tier.pack(side="left")

        # ── Page + slot bar ───────────────────────────────────────────────────
        self.page_frame = tk.Frame(self.root, bg=BG2, pady=4)
        self.page_frame.pack(fill="x")
        self.page_buttons = []  # rebuilt per PDF

        # Text extract button
        tk.Button(self.page_frame, text="T Text",
                  command=self._show_text_popup,
                  bg="#1e3a5f", fg="#7dd3fc",
                  activebackground="#2563eb", activeforeground="#fff",
                  relief="flat", font=("Segoe UI", 8), padx=8, pady=3,
                  cursor="hand2", bd=0).pack(side="left", padx=(4, 10))

        # Slot chips — CLICKABLE buttons to select which slot to draw next
        self._slot_chips = []
        for i, (skey, slabel, scolor, sborder) in enumerate(SLOT_DEFS):
            btn = tk.Button(self.page_frame, text=slabel,
                            command=lambda idx=i: self._set_active_slot(idx),
                            bg="#1e293b", fg="#475569",
                            activebackground="#334155", activeforeground="#fff",
                            font=("Segoe UI", 8, "bold"),
                            padx=8, pady=3, relief="flat", cursor="hand2", bd=0)
            btn.pack(side="left", padx=2)
            self._slot_chips.append(btn)

        tk.Button(self.page_frame, text="X Clear",
                  command=self._clear_active_slot,
                  bg="#1e293b", fg="#64748b",
                  activebackground="#334155", activeforeground="#fff",
                  relief="flat", font=("Segoe UI", 8), padx=6, pady=3,
                  cursor="hand2", bd=0).pack(side="left", padx=(10, 2))

        tk.Button(self.page_frame, text="Clear All",
                  command=self._clear_all_slots,
                  bg="#7f1d1d", fg="#fca5a5",
                  activebackground="#991b1b", activeforeground="#fff",
                  relief="flat", font=("Segoe UI", 8), padx=6, pady=3,
                  cursor="hand2", bd=0).pack(side="left", padx=2)

        # ── PDF canvas ────────────────────────────────────────────────────────
        cf = tk.Frame(self.root, bg=BG)
        cf.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(cf, bg="#1a1a2e", highlightthickness=0,
                                cursor="crosshair")
        vsb = tk.Scrollbar(cf, orient="vertical",   command=self.canvas.yview)
        hsb = tk.Scrollbar(cf, orient="horizontal", command=self.canvas.xview)
        self.canvas.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        vsb.pack(side="right",  fill="y")
        hsb.pack(side="bottom", fill="x")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.canvas.bind("<ButtonPress-1>",   self._sel_press)
        self.canvas.bind("<B1-Motion>",       self._sel_motion)
        self.canvas.bind("<ButtonRelease-1>", self._sel_release)

        # ── Notes panel ───────────────────────────────────────────────────────
        nf = tk.Frame(self.root, bg=BG2, pady=6, padx=10)
        nf.pack(fill="x")
        tk.Label(nf, text="Observations:", bg=BG2, fg=ACC,
                 font=("Segoe UI", 9, "bold")).grid(row=0, column=0,
                 sticky="nw", padx=(0, 8))
        ff = tk.Frame(nf, bg=BG2)
        ff.grid(row=0, column=1, sticky="w")
        self.field_vars = {}
        quick_fields = [
            ("Grid pos",         "grid_position",  ["top-right","top-left","bottom-right","bottom-left","centre","none"]),
            ("Grid size",        "grid_size",       ["large","medium","small","none"]),
            ("STR location",     "str_location",    ["below-grid","top-left","top-right","separate-box","none"]),
            ("STR handwritten",  "str_handwritten", ["yes","no","mixed","na"]),
            ("County loc",       "county_location", ["top-header","stamp","field","none"]),
            ("Lat/lon printed",  "latlon_present",  ["yes","no"]),
        ]
        for col, (label, key, opts) in enumerate(quick_fields):
            tk.Label(ff, text=label+":", bg=BG2, fg="#94a3b8",
                     font=("Segoe UI", 8)).grid(row=0, column=col*2,
                     padx=(6, 2), sticky="e")
            var = tk.StringVar(value="?")
            self.field_vars[key] = var
            ttk.Combobox(ff, textvariable=var, values=["?"]+opts, width=11,
                         font=("Segoe UI", 8), state="readonly"
                         ).grid(row=0, column=col*2+1, padx=(0, 6))
        tk.Label(nf, text="Notes:", bg=BG2, fg="#94a3b8",
                 font=("Segoe UI", 8)).grid(row=1, column=0, sticky="nw",
                 padx=(0, 8), pady=(4, 0))
        self.notes_text = tk.Text(nf, height=2, width=90,
                                  bg="#0f172a", fg=FG, insertbackground=FG,
                                  font=("Segoe UI", 9), relief="flat",
                                  highlightbackground="#334155", highlightthickness=1)
        self.notes_text.grid(row=1, column=1, columnspan=10, sticky="ew", pady=(4, 0))

        # ── Nav bar ───────────────────────────────────────────────────────────
        nav = tk.Frame(self.root, bg="#0d2d52", pady=6, padx=12)
        nav.pack(fill="x")
        bstyle = dict(bg="#1e3a5f", fg="#e2e8f0", activebackground="#2563eb",
                      activeforeground="#fff", relief="flat",
                      font=("Segoe UI", 10, "bold"), padx=14, pady=5,
                      cursor="hand2", bd=0)
        tk.Button(nav, text="< Previous", command=self.prev_pdf, **bstyle
                  ).pack(side="left", padx=4)
        tk.Button(nav, text="Skip", command=self.next_pdf,
                  bg="#1e293b", fg="#64748b", activebackground="#334155",
                  activeforeground="#fff", relief="flat",
                  font=("Segoe UI", 9), padx=10, pady=5,
                  cursor="hand2", bd=0).pack(side="left", padx=4)
        tk.Button(nav, text="<- Inherit",
                  command=self._inherit_from_previous,
                  bg="#164e63", fg="#67e8f9",
                  activebackground="#0e7490", activeforeground="#fff",
                  relief="flat", font=("Segoe UI", 9), padx=10, pady=5,
                  cursor="hand2", bd=0).pack(side="left", padx=4)
        tk.Button(nav, text="Fill Unreviewed",
                  command=self._fill_unreviewed,
                  bg="#1e3a5f", fg="#7dd3fc",
                  activebackground="#2563eb", activeforeground="#fff",
                  relief="flat", font=("Segoe UI", 9), padx=10, pady=5,
                  cursor="hand2", bd=0).pack(side="left", padx=4)
        tk.Button(nav, text="Save Note & Next  >",
                  command=self.save_and_next,
                  bg="#b45309", fg="#fff", activebackground="#d97706",
                  activeforeground="#fff", relief="flat",
                  font=("Segoe UI", 11, "bold"), padx=18, pady=5,
                  cursor="hand2", bd=0).pack(side="right", padx=4)
        tk.Button(nav, text="Next (no note) >>",
                  command=self.next_pdf,
                  bg="#1e3a5f", fg="#90bce0", activebackground="#2563eb",
                  activeforeground="#fff", relief="flat",
                  font=("Segoe UI", 10), padx=14, pady=5,
                  cursor="hand2", bd=0).pack(side="right", padx=4)
        self.lbl_status = tk.Label(nav,
            text="Click chip to pick slot  →  drag to draw  |  X = clear active slot  |  Enter = save+next",
            bg="#0d2d52", fg="#475569", font=("Segoe UI", 8))
        self.lbl_status.pack(side="left", padx=16)

    # ── Key bindings ──────────────────────────────────────────────────────────

    def _bind_keys(self):
        r = self.root
        r.bind("<Right>",      lambda e: self.next_pdf())
        r.bind("<d>",          lambda e: self.next_pdf())
        r.bind("<space>",      lambda e: self.next_pdf())
        r.bind("<Return>",     lambda e: self.save_and_next())
        r.bind("<Left>",       lambda e: self.prev_pdf())
        r.bind("<a>",          lambda e: self.prev_pdf())
        r.bind("<Down>",       lambda e: self.next_page())
        r.bind("<s>",          lambda e: self.next_page())
        r.bind("<Up>",         lambda e: self.prev_page())
        r.bind("<w>",          lambda e: self.prev_page())
        r.bind("<Escape>",     lambda e: self._quit())
        r.bind("<x>",          lambda e: self._clear_active_slot())
        r.bind("<X>",          lambda e: self._clear_active_slot())
        r.bind("<t>",          lambda e: self._show_text_popup())
        r.bind("<T>",          lambda e: self._show_text_popup())
        r.bind("<MouseWheel>", self._mouse_wheel)

    def _mouse_wheel(self, event):
        self.canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

    # ── Slot indicator & active-slot helpers ─────────────────────────────────

    def _set_active_slot(self, idx: int):
        """Click a slot chip to make it the target for the next drag."""
        self._active_slot = idx
        self._update_slot_indicator()

    def _clear_active_slot(self):
        """Remove the rectangle for the currently active slot (X key or X Clear button)."""
        if self._active_slot >= len(SLOT_DEFS):
            return
        skey = SLOT_DEFS[self._active_slot][0]
        if skey in self._slots:
            del self._slots[skey]
        if skey in self._slot_rects:
            self.canvas.delete(self._slot_rects.pop(skey))
        if skey in self._slot_texts:
            self.canvas.delete(self._slot_texts.pop(skey))
        self._update_slot_indicator()

    def _update_slot_indicator(self):
        for i, (skey, slabel, scolor, sborder) in enumerate(SLOT_DEFS):
            chip = self._slot_chips[i]
            if skey in self._slots:
                # Filled — solid color
                slot = self._slots[skey]
                try:
                    w = int(slot.get("w_px", 0))
                    h = int(slot.get("h_px", 0))
                    chip.config(bg=sborder, fg="#fff",
                                text=f"v {slabel} {w}x{h}")
                except (ValueError, TypeError):
                    chip.config(bg=sborder, fg="#fff", text=f"v {slabel}")
            elif i == self._active_slot:
                # Active — draw next here
                chip.config(bg=scolor, fg="#000", text=f"-> {slabel}")
            else:
                # Not yet
                chip.config(bg="#1e293b", fg="#475569", text=slabel)

    # ── Canvas rectangle selection ────────────────────────────────────────────

    def _canvas_to_img(self, cx, cy):
        ox, oy = self._img_offset
        iw, ih = self._img_size
        return (max(0, min(int(cx - ox), iw)),
                max(0, min(int(cy - oy), ih)))

    def _sel_press(self, event):
        cx = self.canvas.canvasx(event.x)
        cy = self.canvas.canvasy(event.y)
        self._sel_start = (cx, cy)
        if self._live_rect:
            self.canvas.delete(self._live_rect)
            self._live_rect = None
        if self._live_text:
            self.canvas.delete(self._live_text)
            self._live_text = None

    def _sel_motion(self, event):
        if self._sel_start is None:
            return
        cx = self.canvas.canvasx(event.x)
        cy = self.canvas.canvasy(event.y)
        x0, y0 = self._sel_start
        skey, slabel, scolor, _ = SLOT_DEFS[self._active_slot]
        if self._live_rect:
            self.canvas.delete(self._live_rect)
        self._live_rect = self.canvas.create_rectangle(
            x0, y0, cx, cy,
            outline=scolor, width=2, dash=(5, 3), tags="live")
        ix0, iy0 = self._canvas_to_img(min(x0, cx), min(y0, cy))
        ix1, iy1 = self._canvas_to_img(max(x0, cx), max(y0, cy))
        hint = f" {slabel}: {ix1-ix0}x{iy1-iy0}px "
        if self._live_text:
            self.canvas.delete(self._live_text)
        ty = min(y0, cy) - 15 if min(y0, cy) > 18 else max(y0, cy) + 4
        self._live_text = self.canvas.create_text(
            min(x0, cx) + 4, ty, text=hint, anchor="nw",
            fill=scolor, font=("Segoe UI", 8, "bold"), tags="live")

    def _sel_release(self, event):
        if self._sel_start is None:
            return
        cx = self.canvas.canvasx(event.x)
        cy = self.canvas.canvasy(event.y)
        x0, y0 = self._sel_start
        self._sel_start = None

        # Clean up live rect
        if self._live_rect:
            self.canvas.delete(self._live_rect)
            self._live_rect = None
        if self._live_text:
            self.canvas.delete(self._live_text)
            self._live_text = None

        rx0, rx1 = sorted([x0, cx])
        ry0, ry1 = sorted([y0, cy])
        if (rx1 - rx0) < 8 or (ry1 - ry0) < 8:
            return  # too small — ignore accidental click

        skey, slabel, scolor, sborder = SLOT_DEFS[self._active_slot]
        ix0, iy0 = self._canvas_to_img(rx0, ry0)
        ix1, iy1 = self._canvas_to_img(rx1, ry1)
        w_px, h_px = ix1 - ix0, iy1 - iy0
        iw, ih = self._img_size
        x_pct = round(ix0 / iw, 4) if iw else 0
        y_pct = round(iy0 / ih, 4) if ih else 0
        w_pct = round(w_px / iw, 4) if iw else 0
        h_pct = round(h_px / ih, 4) if ih else 0

        self._slots[skey] = dict(
            x_px=ix0, y_px=iy0, w_px=w_px, h_px=h_px,
            x_pct=x_pct, y_pct=y_pct, w_pct=w_pct, h_pct=h_pct)

        # Draw permanent rect
        if skey in self._slot_rects:
            self.canvas.delete(self._slot_rects[skey])
        self._slot_rects[skey] = self.canvas.create_rectangle(
            rx0, ry0, rx1, ry1, outline=scolor, width=2, tags="sel")

        # Draw label
        lbl_txt = f" {slabel}: {w_px}x{h_px}px ({w_pct*100:.0f}%x{h_pct*100:.0f}%) "
        ty = ry0 - 16 if ry0 > 20 else ry1 + 4
        if skey in self._slot_texts:
            self.canvas.delete(self._slot_texts[skey])
        self._slot_texts[skey] = self.canvas.create_text(
            rx0 + 4, ty, text=lbl_txt, anchor="nw",
            fill=scolor, font=("Segoe UI", 8, "bold"), tags="sel")

        # Move active pointer to the first empty slot (wrap-search from start)
        first_empty = next(
            (j for j, (sk, *_) in enumerate(SLOT_DEFS) if sk not in self._slots),
            None)
        if first_empty is not None:
            self._active_slot = first_empty
        # else all slots filled — keep _active_slot where it is; user clicks a chip to redraw
        self._update_slot_indicator()

    def _clear_all_slots(self):
        """Remove all slot rectangles and reset."""
        for skey in list(self._slot_rects):
            self.canvas.delete(self._slot_rects.pop(skey))
        for skey in list(self._slot_texts):
            self.canvas.delete(self._slot_texts.pop(skey))
        self._slots.clear()
        self._active_slot = 0
        self._update_slot_indicator()

    # ── Inherit / fill-forward helpers ────────────────────────────────────────

    def _find_nearest_preceding_note(self, from_idx: int):
        """Return (note_dict, path) of the closest preceding entry that has
        at least one slot drawn, or (None, None) if none exists."""
        for i in range(from_idx - 1, -1, -1):
            path = self.entries[i]["local_path"]
            note = self.notes.get(path)
            if note and any(note.get(f"{sk}_w_px") for sk, *_ in SLOT_DEFS):
                return note, path
        return None, None

    def _apply_inherited_slots(self, note: dict):
        """Load all slot rectangles + dropdown fields from *note* into current view."""
        self._slots = {}
        for skey, *_ in SLOT_DEFS:
            try:
                wpx = int(note.get(f"{skey}_w_px") or 0)
            except (ValueError, TypeError):
                wpx = 0
            if wpx > 0:
                self._slots[skey] = {
                    dim: note.get(f"{skey}_{dim}", "")
                    for dim in ["x_px", "y_px", "w_px", "h_px",
                                "x_pct", "y_pct", "w_pct", "h_pct"]
                }
        for key, var in self.field_vars.items():
            val = note.get(key, "")
            if val and val != "?":
                var.set(val)

    def _inherit_from_previous(self):
        """Copy slot rects + fields from the nearest preceding reviewed file.
        Does NOT auto-save — review visually then press Enter / Save+Next."""
        source_note, source_path = self._find_nearest_preceding_note(self.idx)
        if not source_note:
            self.lbl_status.config(text="No preceding note with slot data found.")
            return
        self._apply_inherited_slots(source_note)
        src_name = Path(source_path).name
        # Only set the notes field if it's currently empty
        if not self.notes_text.get("1.0", "end").strip():
            self.notes_text.delete("1.0", "end")
            self.notes_text.insert("1.0", f"inherited:{src_name}")
        self._render_page()
        self.lbl_status.config(
            text=f"Inherited from: {src_name}  —  verify then Save+Next")

    def _fill_unreviewed(self):
        """Bulk-propagate: for every unreviewed entry, copy slot data from the
        nearest preceding reviewed file that has slot data.
        Writes 'inherited:<source>' in the notes field."""
        noted_paths = set(self.notes.keys())
        n_unreviewed = sum(1 for e in self.entries
                          if e["local_path"] not in noted_paths)

        if n_unreviewed == 0:
            self.lbl_status.config(text="All entries already reviewed — nothing to fill.")
            return

        if not messagebox.askyesno(
                "Fill Unreviewed",
                f"Propagate slot data from nearest preceding reviewed file\n"
                f"to {n_unreviewed} unreviewed entries?\n\n"
                f"• Each entry gets notes='inherited:<source_file>'\n"
                f"• Dropdown fields (grid_position, latlon_present, etc.) are copied\n"
                f"• Entries before the very first reviewed file are skipped\n\n"
                f"This CANNOT be undone (make a backup first if unsure)."):
            return

        now_str  = datetime.now().strftime("%Y-%m-%d %H:%M")
        filled   = 0
        skipped  = 0
        last_note = None
        last_path = None

        for e in self.entries:
            path = e["local_path"]
            if path in noted_paths:
                # Already reviewed — update running "last good note" pointer
                note = self.notes[path]
                if any(note.get(f"{sk}_w_px") for sk, *_ in SLOT_DEFS):
                    last_note = note
                    last_path = path
            else:
                # Unreviewed — inherit from last good note if available
                if last_note is None:
                    skipped += 1
                    continue
                src_name = Path(last_path).name
                row = {
                    "local_path":      path,
                    "collection":      e["collection"],
                    "year":            e["year"],
                    "month":           e["month"],
                    "position":        e["position"],
                    "total_in_folder": e.get("total_in_folder", ""),
                    "reviewed_at":     now_str,
                    "notes":           f"inherited:{src_name}",
                }
                for key in self.field_vars:
                    row[key] = last_note.get(key, "?")
                for skey, *_ in SLOT_DEFS:
                    for dim in ["x_px", "y_px", "w_px", "h_px",
                                "x_pct", "y_pct", "w_pct", "h_pct"]:
                        row[f"{skey}_{dim}"] = last_note.get(f"{skey}_{dim}", "")
                self.notes[path] = row
                filled += 1

        if filled:
            self._write_notes()

        parts = [f"Filled {filled} entries with inherited slot data."]
        if skipped:
            parts.append(f"{skipped} skipped (before first reviewed file).")
        parts.append("CSV saved.")
        self.lbl_status.config(text="  ".join(parts))

    # ── OCR text popup ────────────────────────────────────────────────────────

    def _show_text_popup(self):
        e    = self.entries[self.idx]
        path = e["local_path"]
        win  = tk.Toplevel(self.root)
        win.title(f"Text — {Path(path).name}  pg {self.page_idx+1}")
        win.geometry("900x620")
        win.configure(bg="#0f172a")
        win.grab_set()
        BG2, FG, ACC = "#1e293b", "#e2e8f0", "#f59e0b"

        lbl_wait = tk.Label(win,
            text="Running OCR... (10-20s for scanned pages)",
            bg="#0d2d52", fg="#7dd3fc",
            font=("Segoe UI", 11, "bold"), pady=10)
        lbl_wait.pack(fill="x")

        paned = tk.PanedWindow(win, orient="horizontal", bg="#0f172a",
                               sashwidth=6, sashrelief="flat")
        paned.pack(fill="both", expand=True, padx=6, pady=4)

        left = tk.Frame(paned, bg=BG2)
        paned.add(left, minsize=300)
        tk.Label(left, text="Raw OCR text", bg=BG2, fg=ACC,
                 font=("Segoe UI", 9, "bold"), pady=4).pack(fill="x", padx=6)
        txt = tk.Text(left, bg="#0f172a", fg="#cbd5e1",
                      font=("Consolas", 9), wrap="word", relief="flat",
                      highlightthickness=0)
        sb = tk.Scrollbar(left, command=txt.yview)
        txt.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        txt.pack(fill="both", expand=True, padx=4, pady=(0, 4))

        right = tk.Frame(paned, bg=BG2)
        paned.add(right, minsize=260)
        tk.Label(right, text="Detected patterns", bg=BG2, fg=ACC,
                 font=("Segoe UI", 9, "bold"), pady=4).pack(fill="x", padx=6)
        pat_frame = tk.Frame(right, bg=BG2)
        pat_frame.pack(fill="both", expand=True, padx=8, pady=4)

        bot = tk.Frame(win, bg="#0d2d52", pady=6, padx=10)
        bot.pack(fill="x")
        self._popup_data = {}

        def apply_to_note():
            d = self._popup_data
            if d.get("latlon_format") and d["latlon_format"] != "not found":
                self.field_vars["latlon_present"].set("yes")
            elif d.get("latlon_format") == "not found":
                self.field_vars["latlon_present"].set("no")
            win.destroy()

        tk.Button(bot, text="Apply detected to note fields",
                  command=apply_to_note,
                  bg="#b45309", fg="#fff", activebackground="#d97706",
                  relief="flat", font=("Segoe UI", 10, "bold"),
                  padx=16, pady=5, cursor="hand2", bd=0).pack(side="left", padx=4)
        tk.Button(bot, text="Close", command=win.destroy,
                  bg="#1e293b", fg="#94a3b8", relief="flat",
                  font=("Segoe UI", 9), padx=10, pady=5,
                  cursor="hand2", bd=0).pack(side="left", padx=4)
        method_lbl = tk.Label(bot, text="", bg="#0d2d52", fg="#475569",
                              font=("Segoe UI", 8))
        method_lbl.pack(side="right", padx=8)

        def _run():
            text, method = ocr_page(path, self.page_idx)
            patterns = extract_patterns(text)
            self._popup_data = patterns

            def _update():
                lbl_wait.config(
                    text=f"OCR complete  ({method})",
                    fg="#34d399" if method != "none" else "#f87171")
                method_lbl.config(text=f"Source: {method}")
                txt.config(state="normal")
                txt.delete("1.0", "end")
                txt.insert("end", text or "(no text)")
                txt.tag_config("kw",  foreground="#fcd34d", font=("Consolas", 9, "bold"))
                txt.tag_config("loc", foreground="#86efac")
                txt.tag_config("ll",  foreground="#7dd3fc")
                for kw in ["County", "COUNTY", "county"]:
                    _highlight_text(txt, kw, "kw")
                for kw in ["Sec", "Section", "Twp", "Township", "Range", "Rge"]:
                    _highlight_text(txt, kw, "loc")
                for kw in ["Lat", "Lon", "Latitude", "Longitude"]:
                    _highlight_text(txt, kw, "ll")
                txt.config(state="disabled")
                for w in pat_frame.winfo_children():
                    w.destroy()
                ICONS = {
                    "county":         ("County",      "#fcd34d"),
                    "section":        ("Section",     "#86efac"),
                    "township":       ("Township",    "#86efac"),
                    "range":          ("Range",       "#86efac"),
                    "latlon_format":  ("Lat/Lon fmt", "#7dd3fc"),
                    "lat":            ("Latitude",    "#7dd3fc"),
                    "lon":            ("Longitude",   "#7dd3fc"),
                    "lat_raw":        ("Lat raw",     "#7dd3fc"),
                    "lon_raw":        ("Lon raw",     "#7dd3fc"),
                    "dms_samples":    ("DMS coords",  "#7dd3fc"),
                    "dot_nw":         ("Dot NW quad", "#f9a8d4"),
                    "quadrants_seen": ("Quadrants",   "#f9a8d4"),
                }
                for rn, (key, val) in enumerate(patterns.items()):
                    label, color = ICONS.get(key, (key, "#94a3b8"))
                    if isinstance(val, list):
                        val = ", ".join(str(v) for v in val)
                    tk.Label(pat_frame, text=f"{label}:", bg=BG2, fg="#94a3b8",
                             font=("Segoe UI", 9), anchor="w"
                             ).grid(row=rn, column=0, sticky="w", padx=(0, 8), pady=2)
                    tk.Label(pat_frame, text=str(val), bg=BG2, fg=color,
                             font=("Segoe UI", 9, "bold"), anchor="w"
                             ).grid(row=rn, column=1, sticky="w", pady=2)
                if not patterns:
                    tk.Label(pat_frame, text="No patterns detected.\n(Scanned — OCR may be low quality)",
                             bg=BG2, fg="#94a3b8", font=("Segoe UI", 9), justify="left"
                             ).grid(row=0, column=0, columnspan=2, sticky="w")
            win.after(0, _update)

        threading.Thread(target=_run, daemon=True).start()

    # ── Navigation ────────────────────────────────────────────────────────────

    def next_pdf(self):
        if self.idx < len(self.entries) - 1:
            self.idx += 1
            self.page_idx = 0
            self._load_current()
        else:
            messagebox.showinfo("Done!", f"All PDFs reviewed.\nNotes: {self.notes_path}")
            self._quit()

    def prev_pdf(self):
        if self.idx > 0:
            self.idx -= 1
            self.page_idx = 0
            self._load_current()

    def save_and_next(self):
        self._save_note()
        self.next_pdf()

    def next_page(self):
        e = self.entries[self.idx]
        if self.page_idx < page_count(e["local_path"]) - 1:
            self.page_idx += 1
            self._render_page()

    def prev_page(self):
        if self.page_idx > 0:
            self.page_idx -= 1
            self._render_page()

    def jump_to_page(self, pg):
        self.page_idx = pg
        self._render_page()

    # ── Note saving ───────────────────────────────────────────────────────────

    def _save_note(self):
        e = self.entries[self.idx]
        row = {
            "local_path":      e["local_path"],
            "collection":      e["collection"],
            "year":            e["year"],
            "month":           e["month"],
            "position":        e["position"],
            "total_in_folder": e.get("total_in_folder", ""),
            "reviewed_at":     datetime.now().strftime("%Y-%m-%d %H:%M"),
            "notes":           self.notes_text.get("1.0", "end").strip(),
        }
        for key, var in self.field_vars.items():
            row[key] = var.get()
        # Save all 4 slots (empty string if slot not drawn)
        for skey, *_ in SLOT_DEFS:
            slot = self._slots.get(skey, {})
            for dim in ["x_px", "y_px", "w_px", "h_px",
                        "x_pct", "y_pct", "w_pct", "h_pct"]:
                row[f"{skey}_{dim}"] = slot.get(dim, "")

        self.notes[e["local_path"]] = row
        self._write_notes()
        filled = [SLOT_DEFS[i][1] for i in range(len(SLOT_DEFS))
                  if SLOT_DEFS[i][0] in self._slots]
        hint = "  [" + " ".join(filled) + "]" if filled else ""
        self.lbl_status.config(
            text=f"Saved ({len(self.notes)} total){hint}")

    def _write_notes(self):
        fieldnames = (
            ["local_path", "collection", "year", "month", "position",
             "total_in_folder", "reviewed_at"]
            + list(self.field_vars.keys())
            + ["notes"]
            + SEL_FIELDS
        )
        with open(self.notes_path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            w.writeheader()
            w.writerows(self.notes.values())

    # ── Rendering ─────────────────────────────────────────────────────────────

    def _load_current(self):
        e = self.entries[self.idx]
        self.pbar_var.set(self.idx + 1)
        self.lbl_progress.config(text=f"PDF {self.idx+1} / {len(self.entries)}")

        pos_icon = "FIRST" if "FIRST" in e["position"] else "LAST"
        self.lbl_context.config(
            text=f"{pos_icon}  |  {e['collection']}  /  {e['year']}  /  {e['month']}"
                 f"  |  {e.get('total_in_folder','?')} PDFs in folder")

        m = _re.search(r"\d+", e.get("collection", ""))
        cnum = int(m.group()) if m else 0
        tier = TIER_INFO.get(cnum, ("?", "?", "?"))
        self.lbl_tier.config(
            text=f"Tier: {tier[0]}  |  Era: {tier[1]}  |  {tier[2]}")

        # Rebuild page buttons
        for btn in self.page_buttons:
            btn.destroy()
        self.page_buttons.clear()
        n = page_count(e["local_path"])
        for pg in range(n):
            b = tk.Button(self.page_frame, text=f"Pg {pg+1}",
                          command=lambda p=pg: self.jump_to_page(p),
                          bg="#1e3a5f", fg="#90bce0",
                          activebackground="#2563eb", relief="flat",
                          font=("Segoe UI", 9), padx=8, pady=3,
                          cursor="hand2", bd=0)
            b.pack(side="left", padx=2)
            self.page_buttons.append(b)

        # Restore note fields
        existing = self.notes.get(e["local_path"], {})
        self.notes_text.delete("1.0", "end")
        self.notes_text.insert("1.0", existing.get("notes", ""))
        for key, var in self.field_vars.items():
            var.set(existing.get(key, "?"))

        # Restore slot data from saved note
        self._slots = {}
        for skey, *_ in SLOT_DEFS:
            try:
                wpx = int(existing.get(f"{skey}_w_px") or 0)
            except (ValueError, TypeError):
                wpx = 0
            if wpx > 0:
                self._slots[skey] = {
                    dim: existing.get(f"{skey}_{dim}", "")
                    for dim in ["x_px","y_px","w_px","h_px",
                                "x_pct","y_pct","w_pct","h_pct"]
                }
        # Backward compat: old sel_* fields → grid slot
        if "grid" not in self._slots:
            try:
                wpx = int(existing.get("sel_w_px") or 0)
            except (ValueError, TypeError):
                wpx = 0
            if wpx > 0:
                self._slots["grid"] = {
                    "x_px":  existing.get("sel_x_px", ""),
                    "y_px":  existing.get("sel_y_px", ""),
                    "w_px":  existing.get("sel_w_px", ""),
                    "h_px":  existing.get("sel_h_px", ""),
                    "x_pct": existing.get("sel_x_pct", ""),
                    "y_pct": existing.get("sel_y_pct", ""),
                    "w_pct": existing.get("sel_w_pct", ""),
                    "h_pct": existing.get("sel_h_pct", ""),
                }
        # Auto-inherit from nearest preceding note if this file is unreviewed
        # (slots are shown as a scaffold — not saved until user hits Enter)
        _auto_src = None
        if not self._slots and e["local_path"] not in self.notes:
            _src_note, _src_path = self._find_nearest_preceding_note(self.idx)
            if _src_note:
                self._apply_inherited_slots(_src_note)
                _auto_src = Path(_src_path).name

        # Point active slot at the first empty slot (or slot 0 if all filled)
        self._active_slot = next(
            (j for j, (sk, *_) in enumerate(SLOT_DEFS) if sk not in self._slots),
            0)

        self._render_page()

        if _auto_src:
            self.lbl_status.config(
                text=f"Scaffold from: {_auto_src}  —  adjust if needed, Enter to save")
        else:
            self.lbl_status.config(
                text="Click chip to pick slot  ->  drag to draw  |  X = clear active  |  Enter = save+next")

    def _render_page(self):
        e = self.entries[self.idx]
        for i, btn in enumerate(self.page_buttons):
            btn.config(bg="#b45309" if i == self.page_idx else "#1e3a5f",
                       fg="#fff"   if i == self.page_idx else "#90bce0")
        try:
            w = self.canvas.winfo_width() or 900
            photo, (iw, ih), scale = render_page(
                e["local_path"], self.page_idx, max_width=max(w - 40, 600))
        except Exception as exc:
            self.lbl_status.config(text=f"Render error: {exc}")
            return

        self._current_photo = photo
        cx_offset = max((self.canvas.winfo_width() - iw) // 2, 10)
        self._img_offset = (cx_offset, 10)
        self._img_size   = (iw, ih)

        # Clear canvas — kills all old rect/text IDs
        self.canvas.delete("all")
        self._slot_rects = {}
        self._slot_texts = {}

        self.canvas.create_image(cx_offset, 10, anchor="nw", image=photo)
        self.canvas.config(scrollregion=(0, 0, iw + 2*cx_offset, ih + 20))
        self.canvas.yview_moveto(0)

        # Redraw all filled slots
        for skey, slabel, scolor, sborder in SLOT_DEFS:
            slot = self._slots.get(skey)
            if not slot:
                continue
            try:
                ix0  = int(slot["x_px"]);  iy0  = int(slot["y_px"])
                w_px = int(slot["w_px"]);  h_px = int(slot["h_px"])
                rx0  = ix0 + cx_offset;   ry0  = iy0 + 10
                rx1  = rx0 + w_px;        ry1  = ry0 + h_px
                self._slot_rects[skey] = self.canvas.create_rectangle(
                    rx0, ry0, rx1, ry1, outline=scolor, width=2, tags="sel")
                w_pct = float(slot.get("w_pct", 0))
                h_pct = float(slot.get("h_pct", 0))
                lbl_txt = f" {slabel}: {w_px}x{h_px}px ({w_pct*100:.0f}%x{h_pct*100:.0f}%) "
                ty = ry0 - 16 if ry0 > 20 else ry1 + 4
                self._slot_texts[skey] = self.canvas.create_text(
                    rx0 + 4, ty, text=lbl_txt, anchor="nw",
                    fill=scolor, font=("Segoe UI", 8, "bold"), tags="sel")
            except (ValueError, TypeError, KeyError):
                pass

        self._update_slot_indicator()
        self.root.title(
            f"PDF Inspector  [{self.idx+1}/{len(self.entries)}]  "
            f"— {Path(e['local_path']).name}  "
            f"(page {self.page_idx+1}/{len(self.page_buttons)})")

    def _quit(self):
        if messagebox.askyesno("Quit", "Save notes and exit?"):
            if self.notes:
                self._write_notes()
            self.root.destroy()


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="PDF inspection viewer.")
    ap.add_argument("--folder", default=str(Path(__file__).parent.parent / "inspection_pdfs"))
    ap.add_argument("--collections", type=int, nargs="+", metavar="N")
    ap.add_argument("--resume", action="store_true")
    args = ap.parse_args()

    folder     = Path(args.folder)
    notes_path = folder / "_notes.csv"

    if not folder.exists():
        sys.exit(f"Folder not found: {folder}")

    entries = load_manifest(folder, args.collections or [])
    if not entries:
        sys.exit("No PDF entries found.")

    print(f"Loaded {len(entries)} PDFs from {folder}")
    print(f"Notes  -> {notes_path}")
    print("Click a slot chip to select it, then drag to draw that rectangle.")
    print("Any order — draw LAT/LON only, skip GRID, etc.")
    print("X = clear active slot   Enter = save + next\n")

    root = tk.Tk()
    try:
        root.wm_attributes("-alpha", 1.0)
    except Exception:
        pass

    app = InspectionApp(root, entries, notes_path, args.resume)
    root.mainloop()
    if app.notes:
        print(f"\nNotes saved: {notes_path}  ({len(app.notes)} files reviewed)")


if __name__ == "__main__":
    main()
