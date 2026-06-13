"""review_console.py -- inspect sampled records one by one, WITH box annotation.

Mirrors the earlier manual-review workflow: for each record you see the source
PDF and the extracted GRID / LOCATION / COUNTY images + fields, and you can
DRAW boxes around the grid / county / STR / latlong regions, label the format,
add notes, and reuse the previous record's boxes with one click ("Same as
previous"). Everything saves to review_notes.csv (resumable).

Controls
  Region to draw : 1=GRID(green) 2=COUNTY(blue) 3=STR(orange) 4=LATLONG(purple)
  Draw box       : click-drag on the page (replaces that region's box)
  Page           : PageUp / PageDown  (boxes are stored per page)
  Same as prev   : S key or button  -> copies prev record's boxes + format label
  Clear boxes    : C
  Save           : O = OK,  W = WRONG   (writes boxes + format + notes)
  Move record    : Left / Right arrows
  Format label   : type in the Format box (e.g. top_left, latlong, bottom_left)

Usage: python review_console.py [--index D:\\project_outputs_sample\\unseen_index.csv]
"""
import argparse, csv, io, os
from pathlib import Path
import tkinter as tk

import fitz
from PIL import Image, ImageTk

csv.field_size_limit(2_000_000)
OUT = Path(r"D:\project_outputs")
NOTES = OUT / "review_notes.csv"
REGIONS = ["grid", "county", "str", "latlong"]
COLORS = {"grid": "#22c55e", "county": "#3b82f6", "str": "#f59e0b", "latlong": "#a855f7"}
BOXCOLS = [f"{r}_box" for r in REGIONS]
NOTE_COLS = ["pdf_stem", "collection", "year", "month", "verdict", "format_label",
             "notes", "section", "township", "range", "county_name",
             "resolved_lat", "resolved_lon", "resolution_source"] + BOXCOLS
MAXW = 620   # page render width on canvas


def _ledger(only=None):
    """Load ledger rows. If `only` (a set of stems) is given, keep just those —
    avoids holding all 571k rows in RAM so the console is safe to run alongside
    the pipeline on a low-memory machine."""
    m = {}
    p = OUT / "master_ledger.csv"
    if p.exists():
        with p.open(newline="", encoding="utf-8", errors="replace") as f:
            for r in csv.DictReader(f):
                s = r["pdf_stem"]
                if only is None or s in only:
                    m[s] = r
    return m


def _find_pngs(stem):
    out = {}
    for kind, sub in [("grid", "grids"), ("location", "locations"), ("county", "counties")]:
        hits = list((OUT / sub).rglob(f"{stem}*crop.png")) or \
               list((OUT / sub).rglob(f"{stem}*{kind}*.png"))
        if hits:
            out[kind] = hits[0]
    return out


def _load_notes():
    rows = {}
    if NOTES.exists():
        with NOTES.open(newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                rows[r["pdf_stem"]] = r
    return rows


class App:
    def __init__(self, root, items, ledger):
        self.root, self.items, self.led = root, items, ledger
        self.notes_db = _load_notes()
        self.i = 0
        self.page = 0
        self.region = "grid"
        self.boxes = {}            # region -> (page, x0,y0,x1,y1) normalized 0..1
        self.prev_saved = None     # last saved row (for "same as previous")
        self._drag = None
        root.title("Record review console — box annotation")
        root.geometry("1480x940")

        top = tk.Frame(root); top.pack(fill="x")
        self.hdr = tk.Label(top, font=("Consolas", 12, "bold"), anchor="w")
        self.hdr.pack(side="left", padx=8, pady=4)
        for txt, fn in [("◀ Rec", self.prev), ("Rec ▶", self.next),
                        ("OK ✓", lambda: self.save("OK")),
                        ("WRONG ✗", lambda: self.save("WRONG")),
                        ("Same as prev", self.same_prev),
                        ("Clear", self.clear_boxes)]:
            tk.Button(top, text=txt, command=fn).pack(side="right", padx=3, pady=3)

        reg = tk.Frame(root); reg.pack(fill="x")
        tk.Label(reg, text="Draw region:", font=("Consolas", 10)).pack(side="left", padx=6)
        self.reg_lbl = tk.Label(reg, font=("Consolas", 10, "bold")); self.reg_lbl.pack(side="left")
        for n, r in enumerate(REGIONS, 1):
            tk.Button(reg, text=f"{n}={r}", fg=COLORS[r],
                      command=lambda rr=r: self.set_region(rr)).pack(side="left", padx=2)
        tk.Label(reg, text="  Format:", font=("Consolas", 10)).pack(side="left", padx=4)
        self.fmt = tk.Entry(reg, width=18); self.fmt.pack(side="left")
        self.pagelbl = tk.Label(reg, font=("Consolas", 10)); self.pagelbl.pack(side="right", padx=8)

        body = tk.Frame(root); body.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(body, bg="#333", width=MAXW)
        self.canvas.pack(side="left", fill="both", expand=True)
        right = tk.Frame(body, width=800); right.pack(side="right", fill="both")
        right.pack_propagate(False)
        self.img_frame = tk.Frame(right); self.img_frame.pack(fill="x")
        self.fields = tk.Label(right, font=("Consolas", 11), justify="left", anchor="w")
        self.fields.pack(fill="x", padx=6, pady=6)
        tk.Label(right, text="Notes:", anchor="w").pack(fill="x", padx=6)
        self.txt = tk.Text(right, height=8, font=("Consolas", 11))
        self.txt.pack(fill="both", expand=True, padx=6, pady=4)

        self.canvas.bind("<ButtonPress-1>", self.on_press)
        self.canvas.bind("<B1-Motion>", self.on_drag)
        self.canvas.bind("<ButtonRelease-1>", self.on_release)
        root.bind("<Left>", lambda e: self.prev())
        root.bind("<Right>", lambda e: self.next())
        root.bind("<Prior>", lambda e: self.turn(-1))   # PageUp
        root.bind("<Next>", lambda e: self.turn(1))     # PageDown
        for n, r in enumerate(REGIONS, 1):
            root.bind(str(n), lambda e, rr=r: self.set_region(rr))
        root.bind("s", lambda e: self.same_prev()); root.bind("S", lambda e: self.same_prev())
        root.bind("c", lambda e: self.clear_boxes()); root.bind("C", lambda e: self.clear_boxes())
        root.bind("o", lambda e: self.save("OK")); root.bind("O", lambda e: self.save("OK"))
        root.bind("w", lambda e: self.save("WRONG")); root.bind("W", lambda e: self.save("WRONG"))
        self._imgs = []
        self.set_region("grid")
        self.show()

    # ---- region ----
    def set_region(self, r):
        self.region = r
        self.reg_lbl.config(text=r, fg=COLORS[r])

    # ---- pdf render (single page) ----
    def _render(self):
        self.canvas.delete("all"); self._imgs = []
        it = self.items[self.i]
        path = it.get("pdf_path", "")
        try:
            d = fitz.open(path)
            self.npages = d.page_count
            self.page = max(0, min(self.page, self.npages-1))
            pg = d[self.page]
            pm = pg.get_pixmap(matrix=fitz.Matrix(2, 2))
            im = Image.open(io.BytesIO(pm.tobytes("png")))
            sc = MAXW / im.width
            self.pw, self.ph = int(im.width*sc), int(im.height*sc)
            im = im.resize((self.pw, self.ph))
            ti = ImageTk.PhotoImage(im); self._imgs.append(ti)
            self.canvas.config(width=self.pw, height=self.ph,
                               scrollregion=(0, 0, self.pw, self.ph))
            self.canvas.create_image(0, 0, anchor="nw", image=ti)
            d.close()
        except Exception as exc:
            self.npages = 1
            self.canvas.create_text(20, 20, anchor="nw", fill="white",
                                    text=f"PDF render failed: {exc}")
        self.pagelbl.config(text=f"page {self.page+1}/{getattr(self,'npages',1)}")
        self._draw_boxes()

    def _draw_boxes(self):
        self.canvas.delete("box")
        for r, (pg, x0, y0, x1, y1) in self.boxes.items():
            if pg != self.page:
                continue
            self.canvas.create_rectangle(x0*self.pw, y0*self.ph, x1*self.pw, y1*self.ph,
                                         outline=COLORS[r], width=2, tags="box")
            self.canvas.create_text(x0*self.pw+3, y0*self.ph+8, anchor="nw",
                                    text=r, fill=COLORS[r], tags="box",
                                    font=("Consolas", 8, "bold"))

    # ---- box drawing ----
    def on_press(self, e):
        self._drag = (e.x, e.y)

    def on_drag(self, e):
        if not self._drag:
            return
        self._draw_boxes()
        self.canvas.create_rectangle(self._drag[0], self._drag[1], e.x, e.y,
                                     outline=COLORS[self.region], width=2, tags="box")

    def on_release(self, e):
        if not self._drag:
            return
        x0, y0 = self._drag; x1, y1 = e.x, e.y
        self._drag = None
        if abs(x1-x0) < 4 or abs(y1-y0) < 4:
            return
        nx0, nx1 = sorted((max(0, min(x0, x1))/self.pw, max(0, min(x1, x0)+abs(x1-x0))/self.pw))
        ny0, ny1 = sorted((min(y0, y1)/self.ph, max(y0, y1)/self.ph))
        self.boxes[self.region] = (self.page, round(nx0, 4), round(ny0, 4),
                                   round(nx1, 4), round(ny1, 4))
        self._draw_boxes()

    def clear_boxes(self):
        self.boxes = {}; self._draw_boxes()

    def turn(self, d):
        self.page += d; self._render()

    # ---- crops + fields ----
    def _show_crops(self, pngs):
        for w in self.img_frame.winfo_children():
            w.destroy()
        for kind in ("grid", "location", "county"):
            col = tk.Frame(self.img_frame); col.pack(side="left", padx=4)
            tk.Label(col, text=kind, font=("Consolas", 9, "bold")).pack()
            p = pngs.get(kind)
            if p and p.exists():
                im = Image.open(p); im.thumbnail((240, 240))
                ti = ImageTk.PhotoImage(im); self._imgs.append(ti)
                tk.Label(col, image=ti).pack()
            else:
                tk.Label(col, text="(none)", fg="#999").pack()

    def show(self):
        if self.i >= len(self.items):
            self.hdr.config(text="DONE — all reviewed"); return
        it = self.items[self.i]; stem = it["pdf_stem"]
        r = self.led.get(stem, it)
        self.hdr.config(text=f"[{self.i+1}/{len(self.items)}] {it.get('collection','')} "
                             f"{it.get('year','')}/{it.get('month','')}  {stem[:46]}")
        self.page = 0
        # restore prior annotation if any
        prior = self.notes_db.get(stem)
        self.boxes = {}
        self.fmt.delete(0, "end"); self.txt.delete("1.0", "end")
        if prior:
            self._load_boxes_from(prior)
            self.fmt.insert(0, prior.get("format_label", ""))
            self.txt.insert("1.0", prior.get("notes", ""))
        self._render()
        self._show_crops(_find_pngs(stem))
        self.fields.config(text=(
            f"Sec/Twp/Rng : {r.get('section','')} / {r.get('township','')} / {r.get('range','')}\n"
            f"County      : {r.get('county_name','')}\n"
            f"Coords      : {r.get('resolved_lat','')}, {r.get('resolved_lon','')}\n"
            f"Resolution  : {r.get('resolution_source','')}\n"
            f"State       : {r.get('overall_state','')}   mapped={r.get('mapped','')}"))

    def _load_boxes_from(self, row):
        self.boxes = {}
        for r in REGIONS:
            v = (row.get(f"{r}_box") or "").strip()
            if v:
                try:
                    pg, rest = v.split(":")
                    x0, y0, x1, y1 = (float(t) for t in rest.split(","))
                    self.boxes[r] = (int(pg), x0, y0, x1, y1)
                except Exception:
                    pass

    def same_prev(self):
        if not self.prev_saved:
            return
        self._load_boxes_from(self.prev_saved)
        self.fmt.delete(0, "end"); self.fmt.insert(0, self.prev_saved.get("format_label", ""))
        self._draw_boxes()

    def save(self, verdict):
        it = self.items[self.i]; stem = it["pdf_stem"]
        r = self.led.get(stem, it)
        row = {c: r.get(c, it.get(c, "")) for c in NOTE_COLS}
        row.update({"pdf_stem": stem, "verdict": verdict,
                    "format_label": self.fmt.get().strip(),
                    "notes": self.txt.get("1.0", "end").strip()})
        for rg in REGIONS:
            b = self.boxes.get(rg)
            row[f"{rg}_box"] = (f"{b[0]}:{b[1]},{b[2]},{b[3]},{b[4]}" if b else "")
        self.notes_db[stem] = row
        self.prev_saved = row
        with NOTES.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=NOTE_COLS); w.writeheader()
            w.writerows(self.notes_db.values())
        self.next()

    def prev(self):
        self.i = max(0, self.i-1); self.show()

    def next(self):
        self.i += 1; self.show()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--index", default=r"D:\project_outputs_sample\unseen_index.csv")
    a = ap.parse_args()
    with open(a.index, newline="", encoding="utf-8", errors="replace") as f:
        items = list(csv.DictReader(f))
    print(f"{len(items)} records in review queue")
    stems = {r["pdf_stem"] for r in items}
    root = tk.Tk()
    App(root, items, _ledger(only=stems))
    root.mainloop()


if __name__ == "__main__":
    main()
