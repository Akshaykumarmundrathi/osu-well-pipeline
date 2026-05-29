"""
inspect_grids.py  —  Visual grid inspector
==========================================
Shows each extracted grid image alongside the full-page debug image.
Press Y / N / S / Q to label; results are saved to inspection.csv immediately.

Usage:
    cd C:\\Users\\akshay\\Downloads\\project_modular
    python inspect_grids.py

    # Skip already-labelled images and start from the next unlabelled one
    python inspect_grids.py --all     # re-review everything (including labelled)

Keyboard shortcuts in the viewer window:
    Y  or  Right-arrow  —  Correct grid (mark as YES)
    N  or  Delete       —  Wrong detection (mark as NO)
    S  or  Down-arrow   —  Skip (decide later)
    Q  or  Escape       —  Save and quit
    Left-arrow          —  Go back one image

Outputs:
    outputs_1911/inspection.csv   — one row per reviewed grid
"""

import argparse
import csv
import os
import sys
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, simpledialog

from PIL import Image, ImageTk

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
THIS_DIR    = Path(__file__).parent
OUT_DIR     = THIS_DIR / "outputs_1911"
GRID_DIR    = OUT_DIR / "grids"
DEBUG_DIR   = OUT_DIR / "debug"
RESULTS_CSV = OUT_DIR / "results.csv"
INSPECT_CSV = OUT_DIR / "inspection.csv"

INSPECT_FIELDS = [
    "pdf_stem", "pdf_path", "page", "grid_w", "grid_h",
    "method", "correct", "note", "grid_png",
]

# Display sizes (pixels on screen)
GRID_DISPLAY_W  = 420
GRID_DISPLAY_H  = 420
DEBUG_DISPLAY_W = 520
DEBUG_DISPLAY_H = 680
PANEL_PAD       = 16

LABEL_COLORS = {"yes": "#2ecc71", "no": "#e74c3c", "skip": "#f39c12", "": "#888888"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def load_results():
    if not RESULTS_CSV.exists():
        print(f"results.csv not found at {RESULTS_CSV}")
        print("Run  python run_1911.py  first to extract grids.")
        sys.exit(1)
    with RESULTS_CSV.open(encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    return [r for r in rows if r["status"].startswith("detected")]


def load_existing_labels():
    if not INSPECT_CSV.exists():
        return {}
    with INSPECT_CSV.open(encoding="utf-8") as f:
        return {r["pdf_stem"]: r for r in csv.DictReader(f)}


def save_labels(labels: dict):
    INSPECT_CSV.parent.mkdir(parents=True, exist_ok=True)
    with INSPECT_CSV.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=INSPECT_FIELDS)
        writer.writeheader()
        for row in labels.values():
            writer.writerow({k: row.get(k, "") for k in INSPECT_FIELDS})


def fit_image(pil_img, max_w, max_h):
    """Resize preserving aspect ratio to fit within (max_w, max_h)."""
    w, h = pil_img.size
    scale = min(max_w / w, max_h / h, 1.0)
    new_w, new_h = int(w * scale), int(h * scale)
    if new_w < 1 or new_h < 1:
        return pil_img
    return pil_img.resize((new_w, new_h), Image.LANCZOS)


def open_pil(path_str):
    p = Path(path_str) if path_str else None
    if p and p.exists():
        try:
            return Image.open(p).convert("RGB")
        except Exception:
            pass
    return None


def make_placeholder(w, h, msg="No image"):
    img = Image.new("RGB", (w, h), color=(50, 50, 50))
    return img


# ---------------------------------------------------------------------------
# Main GUI
# ---------------------------------------------------------------------------

class InspectorApp:
    def __init__(self, root: tk.Tk, rows: list, labels: dict, review_all: bool):
        self.root    = root
        self.labels  = labels
        self.rows    = rows
        self.idx     = 0   # index into self.queue
        self.saved   = 0

        # Build the queue (unlabelled first, or all if --all)
        if review_all:
            self.queue = rows
        else:
            labelled = set(labels.keys())
            unlabelled = [r for r in rows if r["pdf_stem"] not in labelled]
            self.queue = unlabelled

        self.total = len(self.queue)

        root.title("Grid Inspector")
        root.configure(bg="#1e1e1e")
        root.resizable(True, True)

        self._build_ui()
        self._bind_keys()

        if self.total == 0:
            messagebox.showinfo(
                "All done",
                f"All {len(rows)} detected grids have already been labelled.\n\n"
                "Run with  --all  to re-review everything."
            )
            root.after(100, root.destroy)
            return

        self._show_current()

    # ------------------------------------------------------------------
    def _build_ui(self):
        root = self.root

        # ---- Top bar: progress + counter ----
        top = tk.Frame(root, bg="#1e1e1e")
        top.pack(fill="x", padx=PANEL_PAD, pady=(PANEL_PAD, 0))

        self.lbl_progress = tk.Label(
            top, text="", font=("Consolas", 11), bg="#1e1e1e", fg="#aaaaaa"
        )
        self.lbl_progress.pack(side="left")

        self.lbl_status = tk.Label(
            top, text="", font=("Consolas", 11, "bold"), bg="#1e1e1e", fg="#ffffff"
        )
        self.lbl_status.pack(side="right")

        # ---- Image panels ----
        img_row = tk.Frame(root, bg="#1e1e1e")
        img_row.pack(fill="both", expand=True, padx=PANEL_PAD, pady=PANEL_PAD)

        # Left: grid image
        left = tk.Frame(img_row, bg="#2a2a2a", bd=2, relief="flat")
        left.pack(side="left", fill="both", expand=True, padx=(0, 8))

        tk.Label(left, text="Extracted grid", font=("Consolas", 10),
                 bg="#2a2a2a", fg="#888888").pack(pady=(6, 0))
        self.lbl_grid_img = tk.Label(left, bg="#2a2a2a")
        self.lbl_grid_img.pack(padx=8, pady=8, fill="both", expand=True)

        self.lbl_grid_meta = tk.Label(
            left, text="", font=("Consolas", 9), bg="#2a2a2a", fg="#888888",
            justify="left", wraplength=GRID_DISPLAY_W
        )
        self.lbl_grid_meta.pack(pady=(0, 8))

        # Right: full-page debug image
        right = tk.Frame(img_row, bg="#2a2a2a", bd=2, relief="flat")
        right.pack(side="left", fill="both", expand=True, padx=(8, 0))

        tk.Label(right, text="Full page (bbox in green)", font=("Consolas", 10),
                 bg="#2a2a2a", fg="#888888").pack(pady=(6, 0))
        self.lbl_debug_img = tk.Label(right, bg="#2a2a2a")
        self.lbl_debug_img.pack(padx=8, pady=8, fill="both", expand=True)

        self.lbl_debug_meta = tk.Label(
            right, text="", font=("Consolas", 9), bg="#2a2a2a", fg="#888888",
            justify="left"
        )
        self.lbl_debug_meta.pack(pady=(0, 8))

        # ---- Stem label ----
        self.lbl_stem = tk.Label(
            root, text="", font=("Consolas", 12, "bold"),
            bg="#1e1e1e", fg="#dddddd", wraplength=900
        )
        self.lbl_stem.pack(pady=(0, 8))

        # ---- Buttons ----
        btn_row = tk.Frame(root, bg="#1e1e1e")
        btn_row.pack(pady=(0, PANEL_PAD))

        btn_cfg = dict(font=("Arial", 13, "bold"), width=9, relief="flat", cursor="hand2")

        self.btn_yes = tk.Button(
            btn_row, text="Y  Correct", bg="#27ae60", fg="white",
            activebackground="#2ecc71",
            command=self._on_yes, **btn_cfg
        )
        self.btn_yes.pack(side="left", padx=6)

        self.btn_no = tk.Button(
            btn_row, text="N  Wrong", bg="#c0392b", fg="white",
            activebackground="#e74c3c",
            command=self._on_no, **btn_cfg
        )
        self.btn_no.pack(side="left", padx=6)

        self.btn_skip = tk.Button(
            btn_row, text="S  Skip", bg="#e67e22", fg="white",
            activebackground="#f39c12",
            command=self._on_skip, **btn_cfg
        )
        self.btn_skip.pack(side="left", padx=6)

        tk.Frame(btn_row, width=20, bg="#1e1e1e").pack(side="left")  # spacer

        self.btn_back = tk.Button(
            btn_row, text="← Back", bg="#444444", fg="white",
            activebackground="#666666",
            command=self._on_back, **btn_cfg
        )
        self.btn_back.pack(side="left", padx=6)

        self.btn_quit = tk.Button(
            btn_row, text="Q  Quit", bg="#555555", fg="white",
            activebackground="#777777",
            command=self._on_quit, **btn_cfg
        )
        self.btn_quit.pack(side="left", padx=6)

        # ---- Shortcut hint ----
        tk.Label(
            root,
            text="Keyboard:  Y = correct   N = wrong   S = skip   ← = back   Q / Esc = quit",
            font=("Consolas", 9), bg="#1e1e1e", fg="#555555"
        ).pack(pady=(0, 6))

    # ------------------------------------------------------------------
    def _bind_keys(self):
        r = self.root
        r.bind("<KeyPress-y>",      lambda e: self._on_yes())
        r.bind("<KeyPress-Y>",      lambda e: self._on_yes())
        r.bind("<Right>",           lambda e: self._on_yes())
        r.bind("<KeyPress-n>",      lambda e: self._on_no())
        r.bind("<KeyPress-N>",      lambda e: self._on_no())
        r.bind("<Delete>",          lambda e: self._on_no())
        r.bind("<KeyPress-s>",      lambda e: self._on_skip())
        r.bind("<KeyPress-S>",      lambda e: self._on_skip())
        r.bind("<Down>",            lambda e: self._on_skip())
        r.bind("<Left>",            lambda e: self._on_back())
        r.bind("<KeyPress-q>",      lambda e: self._on_quit())
        r.bind("<KeyPress-Q>",      lambda e: self._on_quit())
        r.bind("<Escape>",          lambda e: self._on_quit())

    # ------------------------------------------------------------------
    def _show_current(self):
        if self.idx >= self.total:
            self._finish()
            return

        row = self.queue[self.idx]
        stem = row["pdf_stem"]

        # Progress
        done    = len([l for l in self.labels.values() if l.get("correct")])
        yes_ct  = sum(1 for l in self.labels.values() if l.get("correct") == "yes")
        no_ct   = sum(1 for l in self.labels.values() if l.get("correct") == "no")
        self.lbl_progress.config(
            text=f"Image {self.idx + 1} / {self.total}   |   "
                 f"labelled: {done}  (Y:{yes_ct}  N:{no_ct})"
        )

        # Current label indicator
        existing = self.labels.get(stem, {})
        cur_label = existing.get("correct", "")
        color = LABEL_COLORS.get(cur_label, "#888888")
        label_text = f"[ {cur_label.upper() or 'unlabelled'} ]"
        self.lbl_status.config(text=label_text, fg=color)

        # Stem
        self.lbl_stem.config(text=stem)

        # Grid image
        grid_pil = open_pil(row.get("grid_png", ""))
        if grid_pil:
            grid_fit = fit_image(grid_pil, GRID_DISPLAY_W, GRID_DISPLAY_H)
        else:
            grid_fit = make_placeholder(GRID_DISPLAY_W, GRID_DISPLAY_H, "grid not found")
        self._grid_photo = ImageTk.PhotoImage(grid_fit)
        self.lbl_grid_img.config(image=self._grid_photo)

        gw = row.get("grid_w", "?")
        gh = row.get("grid_h", "?")
        method = row.get("method", "?")
        ar = round(int(gw) / int(gh), 2) if str(gw).isdigit() and str(gh).isdigit() else "?"
        self.lbl_grid_meta.config(
            text=f"{gw} × {gh} px   AR={ar}   method={method}"
        )

        # Debug image (full page with bbox)
        debug_path = DEBUG_DIR / f"{stem}_debug.jpg"
        debug_pil  = open_pil(str(debug_path))
        if debug_pil:
            debug_fit = fit_image(debug_pil, DEBUG_DISPLAY_W, DEBUG_DISPLAY_H)
        else:
            debug_fit = make_placeholder(DEBUG_DISPLAY_W, DEBUG_DISPLAY_H, "debug img missing")
        self._debug_photo = ImageTk.PhotoImage(debug_fit)
        self.lbl_debug_img.config(image=self._debug_photo)

        pw = row.get("page_w", "?")
        ph = row.get("page_h", "?")
        self.lbl_debug_meta.config(text=f"Page size: {pw} × {ph} px")

    # ------------------------------------------------------------------
    def _record(self, correct: str, note: str = ""):
        row  = self.queue[self.idx]
        stem = row["pdf_stem"]
        self.labels[stem] = {
            "pdf_stem": stem,
            "pdf_path": row.get("pdf_path", ""),
            "page":     row.get("page", ""),
            "grid_w":   row.get("grid_w", ""),
            "grid_h":   row.get("grid_h", ""),
            "method":   row.get("method", ""),
            "correct":  correct,
            "note":     note,
            "grid_png": row.get("grid_png", ""),
        }
        save_labels(self.labels)   # write after every label (crash-safe)
        self.saved += 1
        self.idx += 1
        self._show_current()

    # ------------------------------------------------------------------
    def _on_yes(self):
        self._record("yes")

    def _on_no(self):
        note = simpledialog.askstring(
            "Wrong detection",
            "What's wrong with this detection?\n(e.g. text band, full page, blank)\n\nLeave empty to just mark as wrong.",
            parent=self.root,
        ) or ""
        self._record("no", note)

    def _on_skip(self):
        # Advance without recording a label
        self.idx += 1
        self._show_current()

    def _on_back(self):
        if self.idx > 0:
            self.idx -= 1
            self._show_current()

    def _on_quit(self):
        save_labels(self.labels)
        self.root.destroy()

    # ------------------------------------------------------------------
    def _finish(self):
        yes_ct = sum(1 for l in self.labels.values() if l.get("correct") == "yes")
        no_ct  = sum(1 for l in self.labels.values() if l.get("correct") == "no")
        messagebox.showinfo(
            "Inspection complete",
            f"All {self.total} images reviewed!\n\n"
            f"Correct:  {yes_ct}\n"
            f"Wrong:    {no_ct}\n\n"
            f"Saved to: {INSPECT_CSV}"
        )
        self.root.destroy()


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Visual grid inspector")
    ap.add_argument(
        "--all", action="store_true",
        help="Re-review everything including already-labelled images"
    )
    args = ap.parse_args()

    rows   = load_results()
    labels = load_existing_labels()

    if not rows:
        print("No detected grids found in results.csv.")
        sys.exit(0)

    root = tk.Tk()
    root.geometry("1080x820")
    root.minsize(800, 600)

    app = InspectorApp(root, rows, labels, review_all=args.all)
    root.mainloop()

    # Print summary after GUI closes
    yes_ct = sum(1 for l in labels.values() if l.get("correct") == "yes")
    no_ct  = sum(1 for l in labels.values() if l.get("correct") == "no")
    skip_ct = len([r for r in rows if r["pdf_stem"] not in labels])
    print(f"\nInspection saved to: {INSPECT_CSV}")
    print(f"  Correct : {yes_ct}")
    print(f"  Wrong   : {no_ct}")
    print(f"  Unlabelled remaining: {skip_ct}")
    if no_ct:
        print("\nWrong detections:")
        for l in labels.values():
            if l.get("correct") == "no":
                print(f"  {l['pdf_stem'][:60]}  note: {l['note']}")
