"""review_console.py -- step through sampled records one by one for inspection.

For each record shows: the source PDF (rendered), the extracted GRID / LOCATION
/ COUNTY images, and the extracted fields (Sec/Twp/Rng/county/coords) side by
side, with a notes box. Your notes + verdict save to review_notes.csv
(resumable -- already-noted records are skipped).

Driven by a sample index (default the 1% sample). Reads fields from
master_ledger.csv (fast) and finds output PNGs under the output tree.

Controls:  ←/→ or Prev/Next  ·  Ctrl+S save  ·  records auto-skip once noted
Verdict buttons: OK / WRONG (writes verdict + your typed notes)

Usage:
    python review_console.py [--index D:\\project_outputs_sample\\sample_index.csv]
"""
import argparse, csv, io, os
from pathlib import Path
import tkinter as tk
from tkinter import ttk

import fitz
from PIL import Image, ImageTk

csv.field_size_limit(2_000_000)
OUT = Path(r"D:\project_outputs")
NOTES = OUT / "review_notes.csv"
NOTE_COLS = ["pdf_stem", "collection", "year", "month", "verdict", "notes",
             "section", "township", "range", "county_name",
             "resolved_lat", "resolved_lon", "resolution_source"]


def _ledger():
    m = {}
    p = OUT / "master_ledger.csv"
    if p.exists():
        with p.open(newline="", encoding="utf-8", errors="replace") as f:
            for r in csv.DictReader(f):
                m[r["pdf_stem"]] = r
    return m


def _find_pngs(stem):
    """Locate grid/location/county PNGs for a stem under the output tree."""
    out = {}
    for kind, sub in [("grid", "grids"), ("location", "locations"), ("county", "counties")]:
        hits = list((OUT / sub).rglob(f"{stem}*crop.png")) or \
               list((OUT / sub).rglob(f"{stem}*{kind}*.png"))
        if hits:
            out[kind] = hits[0]
    return out


class App:
    def __init__(self, root, items, ledger):
        self.root, self.items, self.led = root, items, ledger
        self.i = 0
        root.title("Record review console")
        root.geometry("1400x900")
        top = tk.Frame(root); top.pack(fill="x")
        self.hdr = tk.Label(top, font=("Consolas", 12, "bold"), anchor="w")
        self.hdr.pack(side="left", padx=8, pady=4)
        for txt, fn in [("◀ Prev", self.prev), ("Next ▶", self.next),
                        ("OK ✓", lambda: self.save("OK")),
                        ("WRONG ✗", lambda: self.save("WRONG"))]:
            tk.Button(top, text=txt, command=fn).pack(side="right", padx=3, pady=3)

        body = tk.Frame(root); body.pack(fill="both", expand=True)
        self.pdf_canvas = tk.Canvas(body, bg="#333", width=560)
        self.pdf_canvas.pack(side="left", fill="both", expand=True)
        right = tk.Frame(body, width=820); right.pack(side="right", fill="both")
        right.pack_propagate(False)
        self.img_frame = tk.Frame(right); self.img_frame.pack(fill="x")
        self.fields = tk.Label(right, font=("Consolas", 11), justify="left",
                               anchor="w"); self.fields.pack(fill="x", padx=6, pady=6)
        tk.Label(right, text="Your notes:", anchor="w").pack(fill="x", padx=6)
        self.notes = tk.Text(right, height=8, font=("Consolas", 11))
        self.notes.pack(fill="both", expand=True, padx=6, pady=4)

        root.bind("<Left>", lambda e: self.prev())
        root.bind("<Right>", lambda e: self.next())
        root.bind("<Control-s>", lambda e: self.save(""))
        self._imgs = []
        self.show()

    def _render_pdf(self, path):
        self.pdf_canvas.delete("all"); self._imgs = []
        try:
            d = fitz.open(path)
            y = 0; cw = self.pdf_canvas.winfo_width() or 540
            for pg in d:
                pm = pg.get_pixmap(matrix=fitz.Matrix(1.4, 1.4))
                im = Image.open(io.BytesIO(pm.tobytes("png")))
                sc = cw / im.width
                im = im.resize((int(im.width*sc), int(im.height*sc)))
                tkimg = ImageTk.PhotoImage(im); self._imgs.append(tkimg)
                self.pdf_canvas.create_image(0, y, anchor="nw", image=tkimg)
                y += im.height + 6
            self.pdf_canvas.config(scrollregion=(0, 0, cw, y))
            d.close()
        except Exception as exc:
            self.pdf_canvas.create_text(20, 20, anchor="nw", fill="white",
                                        text=f"PDF render failed: {exc}")

    def _show_crops(self, pngs):
        for w in self.img_frame.winfo_children():
            w.destroy()
        for kind in ("grid", "location", "county"):
            col = tk.Frame(self.img_frame); col.pack(side="left", padx=4)
            tk.Label(col, text=kind, font=("Consolas", 9, "bold")).pack()
            p = pngs.get(kind)
            if p and p.exists():
                im = Image.open(p); im.thumbnail((250, 250))
                ti = ImageTk.PhotoImage(im); self._imgs.append(ti)
                tk.Label(col, image=ti).pack()
            else:
                tk.Label(col, text="(none)", fg="#999").pack()

    def show(self):
        if self.i >= len(self.items):
            self.hdr.config(text="DONE — all reviewed"); return
        it = self.items[self.i]
        stem = it["pdf_stem"]
        r = self.led.get(stem, it)
        self.hdr.config(text=f"[{self.i+1}/{len(self.items)}] {it.get('collection','')} "
                             f"{it.get('year','')}/{it.get('month','')}  {stem[:48]}")
        self._render_pdf(it.get("pdf_path", ""))
        self._show_crops(_find_pngs(stem))
        self.fields.config(text=(
            f"Sec/Twp/Rng : {r.get('section','')} / {r.get('township','')} / {r.get('range','')}\n"
            f"County      : {r.get('county_name','')}\n"
            f"Coords      : {r.get('resolved_lat','')}, {r.get('resolved_lon','')}\n"
            f"Resolution  : {r.get('resolution_source','')}\n"
            f"State       : {r.get('overall_state','')}   mapped={r.get('mapped','')}"))
        self.notes.delete("1.0", "end")
        self.notes.insert("1.0", self._prior_note(stem))

    def _prior_note(self, stem):
        if NOTES.exists():
            with NOTES.open(newline="", encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    if row["pdf_stem"] == stem:
                        return row.get("notes", "")
        return ""

    def save(self, verdict):
        it = self.items[self.i]; stem = it["pdf_stem"]
        r = self.led.get(stem, it)
        row = {c: r.get(c, it.get(c, "")) for c in NOTE_COLS}
        row["pdf_stem"] = stem
        row["verdict"] = verdict
        row["notes"] = self.notes.get("1.0", "end").strip()
        rows = []
        if NOTES.exists():
            with NOTES.open(newline="", encoding="utf-8") as f:
                rows = [x for x in csv.DictReader(f) if x["pdf_stem"] != stem]
        rows.append(row)
        with NOTES.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=NOTE_COLS); w.writeheader(); w.writerows(rows)
        self.next()

    def prev(self):
        self.i = max(0, self.i-1); self.show()

    def next(self):
        self.i += 1; self.show()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--index", default=r"D:\project_outputs_sample\sample_index.csv")
    a = ap.parse_args()
    with open(a.index, newline="", encoding="utf-8", errors="replace") as f:
        items = list(csv.DictReader(f))
    print(f"{len(items)} records in review queue")
    root = tk.Tk()
    App(root, items, _ledger())
    root.mainloop()


if __name__ == "__main__":
    main()
