"""
inspect_dots.py -- tkinter click-labeling GUI for dot detection QA
===================================================================

Shows grid PNGs for records where the dot stage FAILED (default: MID form
type, the 7%-success offender) and lets you label the true dot position by
clicking. Labels are written in the exact manual_labels.csv schema that
unet_dot_detector.py consumes for retraining (write_manual_mask /
parse_label_dots), so a labeling session feeds the next training run
directly.

Controls (PREDICT-then-CORRECT workflow):
    The model's predicted dot is shown as an ORANGE ring (if it has one).
    Space/Enter  ACCEPT the prediction as ground truth + next
                 (with your click instead, saves your correction)
    Left-click   correct the dot position (overrides prediction)
    Right-click  add an EXTRA dot (multi-dot forms)
    N            grid has NO visible dot          (dot_count=0)
    G            image is NOT actually a grid      (is_grid=False)
    U            undo clicks (prediction stays available)
    S            skip without saving
    Q / Esc      quit (progress saved)

Usage:
    python inspect_dots.py                          # MID dot-failures
    python inspect_dots.py --form-type T1_LARGE
    python inspect_dots.py --form-type any --status done   # audit successes
    python inspect_dots.py --limit 50

Output:
    $OUTPUT_ROOT/dot_labels/manual_labels.csv      (resumable; appends)
"""
import argparse
import csv
import os
import sys
import tkinter as tk
from pathlib import Path

from PIL import Image, ImageTk

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
csv.field_size_limit(2_000_000)

OUTPUT_ROOT = Path(os.environ.get("OUTPUT_ROOT", r"D:\project_outputs"))
STATUS_CSV  = OUTPUT_ROOT / "processing_status.csv"
LABEL_DIR   = OUTPUT_ROOT / "dot_labels"
LABEL_CSV   = LABEL_DIR / "manual_labels.csv"

LABEL_FIELDS = ["image", "image_path", "is_grid", "x", "y",
                "dot_count", "extra_dots", "form_type", "pdf_stem",
                "pred_x", "pred_y", "pred_conf", "accepted_pred"]


def _load_model():
    """Load the production U-Net once for prediction overlays."""
    import sys as _sys
    _sys.path.insert(0, r"D:\project_modular")
    import torch
    from unet_dot_detector import UNet
    ck = torch.load(r"D:\project_modular\unet_best.pth",
                    map_location="cpu", weights_only=True)
    m = UNet(in_channels=1, out_channels=1,
             features=ck.get("features", [16, 32, 64, 128]))
    m.load_state_dict(ck["model_state"])
    m.eval()
    return m


def _predict_dot(model, pil_img) -> tuple[int, int, float] | None:
    """Best dot guess in original pixels (low threshold — show ANY signal)."""
    import numpy as np, torch, cv2
    w, h = pil_img.size
    gray = pil_img.convert("L").resize((192, 192))
    t = torch.from_numpy(
        np.array(gray, dtype=np.float32) / 255.0).unsqueeze(0).unsqueeze(0)
    with torch.no_grad():
        pred = model(t).squeeze().numpy()
    if pred.max() < 0.15:
        return None
    binary = (pred >= min(0.30, float(pred.max()) * 0.8)).astype(np.uint8)
    n, labels, stats, cents = cv2.connectedComponentsWithStats(binary, 8)
    best = None
    for i in range(1, n):
        conf = float(pred[labels == i].mean())
        if best is None or conf > best[2]:
            best = (int(cents[i][0] * w / 192), int(cents[i][1] * h / 192), conf)
    return best

MAX_DISPLAY = 760   # px -- grid PNGs are scaled up to this for clicking


def load_candidates(form_type: str, status: str, limit: int) -> list[dict]:
    """Records matching the form-type + dot-status filter that have a grid PNG."""
    done = set()
    if LABEL_CSV.exists():
        with LABEL_CSV.open(newline="", encoding="utf-8") as f:
            done = {r["image"] for r in csv.DictReader(f)}

    # Snapshot first — holding the live CSV open blocks the pipeline's
    # atomic rename on Windows (see ISSUES_AND_FIXES.md P7).
    import shutil
    LABEL_DIR.mkdir(parents=True, exist_ok=True)
    snap = LABEL_DIR / "_status_snapshot.csv"
    shutil.copy2(STATUS_CSV, snap)

    out = []
    with snap.open(newline="", encoding="utf-8", errors="replace") as f:
        for r in csv.DictReader(f):
            if status != "any" and r.get("dot_status") != status:
                continue
            if form_type != "any" and r.get("grid_form_type") != form_type:
                continue
            gp = (r.get("grid_image_path") or "").strip()
            if not gp:
                continue
            p = Path(gp)
            if not p.is_absolute():
                p = OUTPUT_ROOT / gp
            if not p.exists() or p.name in done:
                continue
            out.append({"stem": r["pdf_stem"], "path": p,
                        "form_type": r.get("grid_form_type", "")})
            if len(out) >= limit:
                break
    return out


class App:
    def __init__(self, root: tk.Tk, items: list[dict]):
        self.root  = root
        self.items = items
        self.idx   = 0
        self.dots: list[tuple[int, int]] = []   # original-image space
        self.scale = 1.0
        self.saved = 0
        self.pred  = None
        try:
            self.model = _load_model()
            print("model loaded — predictions will be overlaid")
        except Exception as exc:
            print(f"model unavailable ({exc}) — manual labeling only")
            self.model = None

        root.title("Dot inspector")
        self.info = tk.Label(root, font=("Consolas", 11), anchor="w")
        self.info.pack(fill="x")
        self.canvas = tk.Canvas(root, width=MAX_DISPLAY, height=MAX_DISPLAY,
                                bg="#222")
        self.canvas.pack()
        self.help = tk.Label(
            root, anchor="w", fg="#555", font=("Consolas", 9),
            text="click=dot  rclick=extra  N=no-dot  G=not-grid  "
                 "U=undo  Space=save+next  S=skip  Q=quit")
        self.help.pack(fill="x")

        self.canvas.bind("<Button-1>", self.on_click)
        self.canvas.bind("<Button-3>", self.on_rclick)
        for key, fn in [("n", self.mark_no_dot), ("g", self.mark_not_grid),
                        ("u", self.undo), ("s", self.skip), ("q", self.quit),
                        ("<space>", self.save_next), ("<Return>", self.save_next),
                        ("<Escape>", self.quit)]:
            root.bind(key if key.startswith("<") else key, fn)
            if not key.startswith("<"):
                root.bind(key.upper(), fn)

        self.show()

    # -- navigation ----------------------------------------------------------
    def show(self):
        if self.idx >= len(self.items):
            self.info.config(text=f"DONE -- {self.saved} labels saved")
            self.canvas.delete("all")
            return
        item = self.items[self.idx]
        self.dots = []
        self.pred = None
        img = Image.open(item["path"]).convert("RGB")
        self.orig_size = img.size
        self.scale = min(MAX_DISPLAY / img.width, MAX_DISPLAY / img.height)
        disp = img.resize((int(img.width * self.scale),
                           int(img.height * self.scale)), Image.LANCZOS)
        self.tkimg = ImageTk.PhotoImage(disp)
        self.canvas.delete("all")
        self.canvas.config(width=disp.width, height=disp.height)
        self.canvas.create_image(0, 0, anchor="nw", image=self.tkimg)
        # Model prediction overlay (orange ring) — accept with Space,
        # correct with a click, reject with N/G.
        pred_txt = "pred: none"
        if self.model is not None:
            try:
                self.pred = _predict_dot(self.model, img)
            except Exception:
                self.pred = None
            if self.pred:
                px, py, pc = self.pred
                sx, sy = px * self.scale, py * self.scale
                self.canvas.create_oval(sx - 9, sy - 9, sx + 9, sy + 9,
                                        outline="orange", width=3,
                                        tags="pred")
                pred_txt = f"pred: ({px},{py}) conf={pc:.2f}"
        self.info.config(text=f"[{self.idx + 1}/{len(self.items)}] "
                              f"{item['form_type']:<12} {pred_txt}  "
                              f"{item['stem'][:45]}")

    def draw_dots(self):
        self.canvas.delete("dot")
        for x, y in self.dots:
            sx, sy = x * self.scale, y * self.scale
            self.canvas.create_oval(sx - 6, sy - 6, sx + 6, sy + 6,
                                    outline="red", width=2, tags="dot")

    # -- events ---------------------------------------------------------------
    def on_click(self, ev):
        self.dots = [(int(ev.x / self.scale), int(ev.y / self.scale))]
        self.draw_dots()

    def on_rclick(self, ev):
        self.dots.append((int(ev.x / self.scale), int(ev.y / self.scale)))
        self.draw_dots()

    def undo(self, _=None):
        self.dots = []
        self.draw_dots()

    def mark_no_dot(self, _=None):
        self.write_row(is_grid="True", dots=[], dot_count="0")
        self.next()

    def mark_not_grid(self, _=None):
        self.write_row(is_grid="False", dots=[], dot_count="")
        self.next()

    def save_next(self, _=None):
        if self.dots:
            # User clicked a correction — that wins.
            self.write_row(is_grid="True", dots=self.dots,
                           dot_count=str(len(self.dots)))
        elif self.pred:
            # No correction → ACCEPT the model's prediction as ground truth.
            px, py, _ = self.pred
            self.write_row(is_grid="True", dots=[(px, py)],
                           dot_count="1", accepted_pred="1")
        else:
            return  # nothing to save -- use N/G/S instead
        self.next()

    def skip(self, _=None):
        self.next()

    def quit(self, _=None):
        print(f"{self.saved} labels saved -> {LABEL_CSV}")
        self.root.destroy()

    def next(self):
        self.idx += 1
        self.show()

    # -- persistence ------------------------------------------------------------
    def write_row(self, is_grid: str, dots: list, dot_count: str,
                  accepted_pred: str = ""):
        item = self.items[self.idx]
        LABEL_DIR.mkdir(parents=True, exist_ok=True)
        new_file = not LABEL_CSV.exists()
        x, y = (dots[0] if dots else ("", ""))
        extra = ";".join(f"{ex}:{ey}" for ex, ey in dots[1:])
        px, py, pc = self.pred if self.pred else ("", "", "")
        with LABEL_CSV.open("a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=LABEL_FIELDS)
            if new_file:
                w.writeheader()
            w.writerow({"image": item["path"].name,
                        "image_path": str(item["path"]),
                        "is_grid": is_grid, "x": x, "y": y,
                        "dot_count": dot_count, "extra_dots": extra,
                        "form_type": item["form_type"],
                        "pdf_stem": item["stem"],
                        "pred_x": px, "pred_y": py,
                        "pred_conf": round(pc, 3) if pc != "" else "",
                        "accepted_pred": accepted_pred})
        self.saved += 1


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--form-type", default="MID",
                    help="grid_form_type filter (MID/T2_MED/T1_LARGE/... or 'any')")
    ap.add_argument("--status", default="failed",
                    help="dot_status filter (failed/done/any)")
    ap.add_argument("--limit", type=int, default=200)
    args = ap.parse_args()

    items = load_candidates(args.form_type, args.status, args.limit)
    print(f"{len(items)} unlabeled candidates "
          f"(form_type={args.form_type}, dot_status={args.status})")
    if not items:
        return
    root = tk.Tk()
    App(root, items)
    root.mainloop()


if __name__ == "__main__":
    main()
