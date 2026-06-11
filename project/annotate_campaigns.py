"""
annotate_campaigns.py -- keyboard-first annotator for review campaigns
=======================================================================

Renders page 1 of each sampled PDF and captures exactly the features the
open issues need. Designed for SPEED: one hand on mouse (boxes), one on
keyboard (categoricals), auto-advance on save. Resumable — already-annotated
stems are skipped on relaunch.

WHAT TO ANNOTATE (the schema answers tonight's open questions):

  Boxes (drag; active field shown in title bar — cycle with TAB):
    G  grid box        — where IS the grid (C8: detector finds it only 48%)
    S  STR box         — where IS sec/twp/rge (C8 loc 33%; early-30s dip)
    C  county box      — C6/C7/C11 county misses; C12 county 8%
    L  lat/lon box     — C12: printed coordinates found only 37% (why?)

  Categoricals (single keys, toggle):
    1  grid printed on form?         y/n   (C8: absent vs missed)
    2  STR labels printed?           y/n   (vs handwritten-only)
    3  twp/rng DIRECTION suffix printed? y/n  (drives suffix inference —
                                            105/400 exposure dots resolved,
                                            most failures = missing suffix)
    4  county printed?               y/n
    5  lat/lon printed?              y/n   (C12 anomaly)
    6  values handwritten?           y/n
    7  multi-well / amended form?    y/n   (suspected C12 confounder)

  Free text:
    F  form id (type what's printed, e.g. 1002A; ESC to cancel)
    N  note

  Navigation:
    Space/Enter  save + next      U  clear boxes     B  back
    X  unreadable/skip            Q  quit (progress saved)
    A  SAME AS PREVIOUS — adopts the prior file's boxes + toggles + form id
       and auto-advances. The previous boxes are shown as gray dashed GHOSTS
       on every new page: if they line up with this form, just hit A.
       (Forms repeat for years at a stretch — this is the fast path.)

Usage:
    python annotate_campaigns.py --campaign c8_layout
    python annotate_campaigns.py --campaign c12_modern --start-at 50

Output:
    D:\\review_campaigns\\{campaign}\\annotations.csv
"""
import argparse
import csv
import sys
import tkinter as tk
from tkinter import simpledialog
from pathlib import Path

from PIL import Image, ImageTk

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(r"D:\review_campaigns")
MAX_W, MAX_H = 980, 760

BOX_FIELDS = {"g": "grid", "s": "str", "c": "county", "l": "latlon"}
BOX_COLORS = {"grid": "red", "str": "blue", "county": "green",
              "latlon": "purple"}
CAT_FIELDS = {"1": "grid_printed", "2": "str_labels_printed",
              "3": "suffix_printed", "4": "county_printed",
              "5": "latlon_printed", "6": "handwritten",
              "7": "multiwell_amended"}

FIELDS = (["pdf_stem", "pdf_path", "collection", "year", "month",
           "form_id", "note", "status"]
          + list(CAT_FIELDS.values())
          + [f"{b}_{c}" for b in BOX_FIELDS.values()
             for c in ("x_pct", "y_pct", "w_pct", "h_pct")])


def _render_page1(pdf_path: str) -> Image.Image | None:
    try:
        import fitz
        doc = fitz.open(pdf_path)
        pix = doc[0].get_pixmap(matrix=fitz.Matrix(1.5, 1.5))
        img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        doc.close()
        return img
    except Exception:
        return None


class App:
    def __init__(self, root: tk.Tk, campaign: str, items: list[dict],
                 done: set[str]):
        self.root = root
        self.campaign = campaign
        self.items = [i for i in items if i["pdf_stem"] not in done]
        self.idx = 0
        self.saved = 0
        self.active_box = "grid"
        self.boxes: dict[str, tuple] = {}     # field -> (x,y,w,h) pct
        self.cats: dict[str, str] = {}
        self.form_id = ""
        self.note = ""
        self._drag = None
        self.out_csv = ROOT / campaign / "annotations.csv"
        # Last SAVED annotation — ghosted onto each new page; key A adopts it.
        self.prev_boxes: dict[str, tuple] = {}
        self.prev_cats: dict[str, str] = {}
        self.prev_form_id = ""
        self._load_prev_from_csv()

        self.info = tk.Label(root, font=("Consolas", 11), anchor="w")
        self.info.pack(fill="x")
        self.canvas = tk.Canvas(root, width=MAX_W, height=MAX_H, bg="#161616")
        self.canvas.pack()
        self.status = tk.Label(root, font=("Consolas", 9), anchor="w",
                               fg="#666")
        self.status.pack(fill="x")

        self.canvas.bind("<ButtonPress-1>", self.on_press)
        self.canvas.bind("<B1-Motion>", self.on_drag)
        self.canvas.bind("<ButtonRelease-1>", self.on_release)
        root.bind("<Tab>", self.cycle_box)
        for k in list(BOX_FIELDS) + [k.upper() for k in BOX_FIELDS]:
            root.bind(k, self.set_box_field)
        for k in CAT_FIELDS:
            root.bind(k, self.toggle_cat)
        root.bind("f", self.ask_form_id);  root.bind("F", self.ask_form_id)
        root.bind("n", self.ask_note);     root.bind("N", self.ask_note)
        root.bind("a", self.same_as_prev); root.bind("A", self.same_as_prev)
        root.bind("u", self.clear_boxes);  root.bind("U", self.clear_boxes)
        root.bind("x", self.mark_skip);    root.bind("X", self.mark_skip)
        root.bind("b", self.go_back);      root.bind("B", self.go_back)
        root.bind("<space>", self.save_next)
        root.bind("<Return>", self.save_next)
        root.bind("q", self.quit);         root.bind("Q", self.quit)
        root.bind("<Escape>", self.quit)
        self.show()

    def _load_prev_from_csv(self):
        """Seed ghosts from the most recent saved row (resume sessions)."""
        if not self.out_csv.exists():
            return
        try:
            rows = list(csv.DictReader(
                self.out_csv.open(newline="", encoding="utf-8")))
            for r in reversed(rows):
                if r.get("status") != "ok":
                    continue
                for bf in BOX_FIELDS.values():
                    try:
                        x = float(r[f"{bf}_x_pct"]); y = float(r[f"{bf}_y_pct"])
                        w = float(r[f"{bf}_w_pct"]); h = float(r[f"{bf}_h_pct"])
                        self.prev_boxes[bf] = (x, y, w, h)
                    except (ValueError, KeyError):
                        continue
                self.prev_cats = {f: r.get(f, "") for f in CAT_FIELDS.values()
                                  if r.get(f, "")}
                self.prev_form_id = r.get("form_id", "")
                break
        except Exception:
            pass

    # -- display -----------------------------------------------------------
    def show(self):
        self.boxes, self.cats = {}, {}
        self.form_id = self.note = ""
        if self.idx >= len(self.items):
            self.info.config(text=f"CAMPAIGN DONE — {self.saved} annotated")
            self.canvas.delete("all")
            return
        item = self.items[self.idx]
        img = _render_page1(item["pdf_path"])
        if img is None:
            self.mark_skip()
            return
        self.scale = min(MAX_W / img.width, MAX_H / img.height)
        disp = img.resize((int(img.width * self.scale),
                           int(img.height * self.scale)), Image.LANCZOS)
        self.img_w, self.img_h = img.size
        self.tkimg = ImageTk.PhotoImage(disp)
        self.canvas.delete("all")
        self.canvas.config(width=disp.width, height=disp.height)
        self.canvas.create_image(0, 0, anchor="nw", image=self.tkimg)
        # Ghost the previous annotation's boxes (dashed) for instant
        # visual same-format check — hit A to adopt them wholesale.
        for bf, (x, y, w, h) in self.prev_boxes.items():
            gx0, gy0 = x * disp.width, y * disp.height
            gx1, gy1 = (x + w) * disp.width, (y + h) * disp.height
            self.canvas.create_rectangle(
                gx0, gy0, gx1, gy1, outline="#999999", width=1,
                dash=(4, 3), tags="ghost")
            self.canvas.create_text(
                gx0 + 3, gy0 - 8, anchor="w", text=bf, fill="#999999",
                font=("Consolas", 8), tags="ghost")
        self.refresh_labels()

    def refresh_labels(self):
        item = self.items[self.idx]
        self.root.title(f"[{self.campaign}] active box: "
                        f"{self.active_box.upper()} (TAB/G/S/C/L to switch)")
        self.info.config(
            text=f"[{self.idx + 1}/{len(self.items)}] C{item['collection']} "
                 f"{item['year']}/{item['month'][:2]}  {item['pdf_stem'][:55]}")
        cats = "  ".join(f"{k}:{CAT_FIELDS[k][:12]}={self.cats.get(CAT_FIELDS[k],'-')}"
                         for k in sorted(CAT_FIELDS))
        self.status.config(
            text=f"boxes:{','.join(self.boxes) or '-'}  form_id:{self.form_id or '-'}  {cats}")

    # -- box drawing ---------------------------------------------------------
    def on_press(self, ev):
        self._drag = (ev.x, ev.y)

    def on_drag(self, ev):
        if not self._drag:
            return
        self.canvas.delete("rubber")
        self.canvas.create_rectangle(*self._drag, ev.x, ev.y,
                                     outline=BOX_COLORS[self.active_box],
                                     width=2, tags="rubber")

    def on_release(self, ev):
        if not self._drag:
            return
        x0, y0 = self._drag
        x1, y1 = ev.x, ev.y
        self._drag = None
        if abs(x1 - x0) < 6 or abs(y1 - y0) < 6:
            return
        sx0, sx1 = sorted((x0, x1))
        sy0, sy1 = sorted((y0, y1))
        dw, dh = self.img_w * self.scale, self.img_h * self.scale
        self.boxes[self.active_box] = (round(sx0 / dw, 4), round(sy0 / dh, 4),
                                       round((sx1 - sx0) / dw, 4),
                                       round((sy1 - sy0) / dh, 4))
        self.canvas.delete("rubber")
        self.canvas.create_rectangle(sx0, sy0, sx1, sy1,
                                     outline=BOX_COLORS[self.active_box],
                                     width=2, tags=f"box_{self.active_box}")
        self.refresh_labels()

    def cycle_box(self, _=None):
        order = list(BOX_FIELDS.values())
        self.active_box = order[(order.index(self.active_box) + 1) % len(order)]
        self.refresh_labels()
        return "break"

    def set_box_field(self, ev):
        self.active_box = BOX_FIELDS[ev.char.lower()]
        self.refresh_labels()

    def clear_boxes(self, _=None):
        self.boxes = {}
        for f in BOX_FIELDS.values():
            self.canvas.delete(f"box_{f}")
        self.refresh_labels()

    # -- categoricals / text ---------------------------------------------------
    def toggle_cat(self, ev):
        f = CAT_FIELDS[ev.char]
        cur = self.cats.get(f, "")
        self.cats[f] = {"": "y", "y": "n", "n": ""}[cur]
        self.refresh_labels()

    def ask_form_id(self, _=None):
        v = simpledialog.askstring("form id", "printed form id:",
                                   parent=self.root)
        if v is not None:
            self.form_id = v.strip()
        self.refresh_labels()

    def ask_note(self, _=None):
        v = simpledialog.askstring("note", "note:", parent=self.root)
        if v is not None:
            self.note = v.strip()
        self.refresh_labels()

    # -- persistence -----------------------------------------------------------
    def _write(self, status: str):
        item = self.items[self.idx]
        row = {k: "" for k in FIELDS}
        row.update(pdf_stem=item["pdf_stem"], pdf_path=item["pdf_path"],
                   collection=item["collection"], year=item["year"],
                   month=item["month"], form_id=self.form_id,
                   note=self.note, status=status, **self.cats)
        for bf, (x, y, w, h) in self.boxes.items():
            row[f"{bf}_x_pct"], row[f"{bf}_y_pct"] = x, y
            row[f"{bf}_w_pct"], row[f"{bf}_h_pct"] = w, h
        new = not self.out_csv.exists()
        with self.out_csv.open("a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS)
            if new:
                w.writeheader()
            w.writerow(row)
        self.saved += 1

    def same_as_prev(self, _=None):
        """Adopt the previous annotation wholesale and advance."""
        if not self.prev_boxes:
            return
        self.boxes   = dict(self.prev_boxes)
        self.cats    = dict(self.prev_cats)
        self.form_id = self.prev_form_id
        if not self.note:
            self.note = "same_as_prev"
        self._write("ok")
        self.idx += 1
        self.show()

    def save_next(self, _=None):
        self._write("ok")
        # Remember what was saved — becomes the ghost for the next pages.
        self.prev_boxes   = dict(self.boxes)
        self.prev_cats    = dict(self.cats)
        self.prev_form_id = self.form_id
        self.idx += 1
        self.show()

    def mark_skip(self, _=None):
        self._write("skip")
        self.idx += 1
        self.show()

    def go_back(self, _=None):
        if self.idx > 0:
            self.idx -= 1
            self.show()

    def quit(self, _=None):
        print(f"{self.saved} annotations saved -> {self.out_csv}")
        self.root.destroy()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--campaign", required=True)
    ap.add_argument("--start-at", type=int, default=0)
    args = ap.parse_args()

    idx_csv = ROOT / args.campaign / "index.csv"
    if not idx_csv.exists():
        print(f"No manifest at {idx_csv} — run build_review_campaigns.py first")
        sys.exit(1)
    items = list(csv.DictReader(idx_csv.open(newline="", encoding="utf-8")))

    done: set[str] = set()
    ann = ROOT / args.campaign / "annotations.csv"
    if ann.exists():
        done = {r["pdf_stem"] for r in csv.DictReader(ann.open(newline="", encoding="utf-8"))}
    print(f"{args.campaign}: {len(items)} sampled, {len(done)} already annotated")

    root = tk.Tk()
    App(root, args.campaign, items[args.start_at:], done)
    root.mainloop()


if __name__ == "__main__":
    main()
