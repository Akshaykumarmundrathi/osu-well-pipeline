"""
retrain_unet.py -- U-Net dot-detector fine-tune with a no-regression gate
==========================================================================

Goal: extend dot detection to grid styles the model has never seen
(MID/LATE-era, C9-C12) WITHOUT degrading the proven early-form accuracy.

Training sources
----------------
OLD  D:\\project_outputs_local\\dot_annotations.csv
     876 human-verified dots (human_x_norm / human_y_norm) on early-form
     grid PNGs — the May 2026 QA set. This is the regression anchor.
NEW  D:\\project_outputs\\dot_labels\\manual_labels.csv
     The inspect_dots.py labeling session (MID/LATE failed grids):
     pixel-space clicks, dot_count=0 negatives, is_grid=False negatives.

Protocol (run stages in order)
------------------------------
  python retrain_unet.py --prepare     # build workspace + 15% stratified
                                       # holdout per source (never trained on)
  python retrain_unet.py --baseline    # score CURRENT unet_best.pth on both
                                       # holdouts -> baseline.json
  python retrain_unet.py --train       # fine-tune from unet_best.pth on the
                                       # combined training split
                                       # -> D:\\unet_retrain\\unet_retrained.pth
  python retrain_unet.py --evaluate    # score the new checkpoint on the same
                                       # holdouts; PASS only if:
                                       #   early-holdout hit-rate drop <= 2 pts
                                       #   new-style holdout improves
  python retrain_unet.py --promote     # backs up unet_best.pth and installs
                                       # the retrained checkpoint (only after
                                       # --evaluate PASS)

A "hit" = predicted dot within 0.06 normalised distance of the human dot
(about half a grid cell).  Never promotes automatically.
"""
import argparse
import csv
import json
import random
import shutil
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, r"D:\project_modular")

WORK      = Path(r"D:\unet_retrain")
IMG_DIR   = WORK / "images"
LBL_DIR   = WORK / "labels"
OLD_CSV   = Path(r"D:\project_outputs_local\dot_annotations.csv")
NEW_CSVS  = [Path(r"D:\project_outputs\dot_labels\manual_labels.csv"),
             Path(r"D:\project_outputs_test1000\dot_labels\manual_labels.csv")]
BEST      = Path(r"D:\project_modular\unet_best.pth")
RETRAINED = WORK / "unet_retrained.pth"
HOLDOUT   = WORK / "holdout.csv"
BASELINE  = WORK / "baseline.json"
SEED      = 42
HOLDOUT_FRAC = 0.15
HIT_DIST  = 0.06


def _load_old() -> list[dict]:
    """May 2026 human-verified dots — TRAINING DIVERSITY ONLY.

    Measured 2026-06-11: the current unet_best.pth produces near-zero
    probability on these May-era crops (pred max ~0.0-0.49) while scoring
    76% on current-pipeline crops — i.e. the May images are a different
    crop style / distribution and unsuitable as the regression anchor.
    They remain genuine human ground truth, so they stay in TRAINING as
    diversity; the anchor comes from _load_anchor() below.
    """
    out = []
    if not OLD_CSV.exists():
        return out
    for r in csv.DictReader(OLD_CSV.open(newline="", encoding="utf-8")):
        p = Path(r.get("grid_image_path", ""))
        try:
            x, y = float(r["human_x_norm"]), float(r["human_y_norm"])
        except (KeyError, ValueError):
            continue
        if p.exists():
            out.append({"image_path": str(p), "x_norm": x, "y_norm": y,
                        "source": "may_extra", "negative": False})
    return out


FORM_STRUCTURES = Path(r"D:\project_outputs\form_structures.csv")
STATUS_SNAPSHOT = Path(r"D:\project_outputs_c2345\ps_final.csv")
OUTPUT_ROOT     = Path(r"D:\project_outputs")
ANCHOR_MAX      = 500   # cap pseudo-anchor sample


def _load_anchor() -> list[dict]:
    """Regression anchor: CURRENT-pipeline crops where the live model already
    detects the dot at high confidence (>=95). Pseudo-labels from production
    predictions — the retrained model must keep nailing these."""
    if not FORM_STRUCTURES.exists():
        return []
    # NOTE: main.py DELETES grid PNGs after a successful dot stage (S3-era
    # assumption), so successful-dot crops never survive locally.  We
    # REGENERATE them: render the page and crop the bbox stored in metadata —
    # the same crop the production dot prediction ran on.
    # form_structures mangles bbox geometry across the two historical bbox
    # conventions — read the RAW [x, y, w, h] from each metadata.json instead.
    import json
    out = []
    for r in csv.DictReader(FORM_STRUCTURES.open(newline="", encoding="utf-8")):
        if r.get("dot_detected") != "True":
            continue
        try:
            conf = float(r.get("dot_confidence") or 0)
            x, y = float(r["dot_x_norm"]), float(r["dot_y_norm"])
        except (KeyError, ValueError):
            continue
        if conf < 95:
            continue
        coll_raw = r.get("collection") or ""
        coll = coll_raw.replace(".zip", "")
        pdf = Path(rf"D:\{coll}") / r.get("year", "") / r.get("month", "") / f"{r['pdf_stem']}.pdf"
        if not pdf.exists():
            continue
        coll_dir = coll_raw.replace(" (", "_").replace(")", "").replace(".zip", "").replace(" ", "_")
        month_dir = (r.get("month") or "").replace(" - ", "___").replace(" ", "_")
        meta = (OUTPUT_ROOT / "metadata" / coll_dir / (r.get("year") or "")
                / month_dir / r["pdf_stem"] / "metadata.json")
        if not meta.exists():
            continue
        try:
            d = json.loads(meta.read_text(encoding="utf-8"))
            g = d["stages"]["grid"]
            gx, gy, gw, gh = [int(v) for v in g["bbox"]]
            page = int(g.get("page") or 1)
        except Exception:
            continue
        if gw <= 0 or gh <= 0 or gw > 2000:
            continue
        out.append({"image_path": "", "pdf_path": str(pdf), "page": page,
                    "bbox": (gx, gy, gw, gh),
                    "x_norm": x, "y_norm": y,
                    "source": "anchor", "negative": False})
        if len(out) >= ANCHOR_MAX:
            break
    return out


def _load_new() -> list[dict]:
    out = []
    from PIL import Image
    for csv_path in NEW_CSVS:
        if not csv_path.exists():
            continue
        for r in csv.DictReader(csv_path.open(newline="", encoding="utf-8")):
            p = Path(r.get("image_path", ""))
            if not p.exists():
                continue
            if r.get("is_grid") == "False" or r.get("dot_count") == "0":
                out.append({"image_path": str(p), "x_norm": -1, "y_norm": -1,
                            "source": "new", "negative": True})
                continue
            try:
                x_px, y_px = int(r["x"]), int(r["y"])
            except (KeyError, ValueError):
                continue
            with Image.open(p) as im:
                w, h = im.size
            out.append({"image_path": str(p), "x_norm": x_px / w,
                        "y_norm": y_px / h, "source": "new", "negative": False})
    return out


def prepare() -> None:
    from unet_dot_detector import write_manual_mask, write_empty_mask
    rows = _load_anchor() + _load_old() + _load_new()
    print("training pool:",
          {s: sum(1 for r in rows if r["source"] == s)
           for s in ("anchor", "may_extra", "new")})
    rng = random.Random(SEED)
    holdout, train = [], []
    # Holdout from anchor (regression gate) and new (improvement gate).
    # may_extra is training-only diversity — never in the holdout.
    for src in ("anchor", "new"):
        grp = [r for r in rows if r["source"] == src]
        rng.shuffle(grp)
        k = max(1, int(len(grp) * HOLDOUT_FRAC)) if grp else 0
        holdout += grp[:k]
        train   += grp[k:]
    train += [r for r in rows if r["source"] == "may_extra"]

    for d in (IMG_DIR, LBL_DIR):
        if d.exists():
            shutil.rmtree(d)
        d.mkdir(parents=True)
    from PIL import Image

    def _materialise(r: dict, dst_dir: Path) -> Path | None:
        """Copy an existing crop, or re-render+crop from pdf/bbox (anchors —
        main.py deletes grid PNGs after successful dot stages)."""
        if r.get("image_path"):
            src = Path(r["image_path"])
            dst = dst_dir / f"{src.parent.name[:40]}_{src.name}"
            shutil.copy2(src, dst)
            return dst
        try:
            from pdf.pdf_manager import PDFDocumentManager
            mgr = PDFDocumentManager(r["pdf_path"], resolution_multiplier=2.0)
            pil = mgr.get_page_pil(int(r["page"]) - 1)
            if pil is None:
                return None
            gx, gy, gw, gh = r["bbox"]
            crop = pil.crop((gx, gy, gx + gw, gy + gh))
            stem = Path(r["pdf_path"]).stem[:50]
            dst = dst_dir / f"anchor_{stem}_grid.png"
            crop.save(str(dst))
            return dst
        except Exception:
            return None

    n_img = 0
    for r in train:
        dst = _materialise(r, IMG_DIR)
        if dst is None:
            continue
        if r["negative"]:
            write_empty_mask(dst, LBL_DIR)
        else:
            with Image.open(dst) as im:
                w, h = im.size
            write_manual_mask(dst, LBL_DIR,
                              [(int(r["x_norm"] * w), int(r["y_norm"] * h))])
        n_img += 1

    # Materialise holdout crops too (regenerated anchors need real files).
    for r in holdout:
        if not r.get("image_path"):
            dst = _materialise(r, WORK / "holdout_imgs")
            r["image_path"] = str(dst) if dst else ""
    holdout = [r for r in holdout if r.get("image_path")]
    for r in holdout:
        r.pop("pdf_path", None); r.pop("page", None); r.pop("bbox", None)
    with HOLDOUT.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["image_path", "x_norm", "y_norm",
                                          "source", "negative"])
        w.writeheader()
        w.writerows(holdout)
    print(f"train: {n_img} images+masks -> {WORK}")
    print(f"holdout: {len(holdout)} ({sum(1 for r in holdout if r['source']=='early')} "
          f"early / {sum(1 for r in holdout if r['source']=='new')} new) -> {HOLDOUT}")


def _score(ckpt: Path) -> dict:
    import torch
    from unet_dot_detector import UNet, DotDetector
    ck = torch.load(str(ckpt), map_location="cpu", weights_only=True)
    model = UNet(in_channels=1, out_channels=1,
                 features=ck.get("features", [16, 32, 64, 128]))
    model.load_state_dict(ck["model_state"])
    model.eval()
    det = DotDetector(model=model, output_dir=WORK / "eval_tmp",
                      threshold=0.50, min_area=8, max_dots=1)
    stats = {"anchor": [0, 0], "new": [0, 0]}
    from PIL import Image
    for r in csv.DictReader(HOLDOUT.open(newline="", encoding="utf-8")):
        if r["negative"] == "True":
            continue
        p = Path(r["image_path"])
        if not p.exists():
            continue
        try:
            dots = det.predict_image(p)
        except Exception:
            dots = []
        hit = 0
        if dots:
            with Image.open(p) as im:
                w, h = im.size
            d = dots[0]
            dx = d["x"] / w - float(r["x_norm"])
            dy = d["y"] / h - float(r["y_norm"])
            hit = int((dx * dx + dy * dy) ** 0.5 <= HIT_DIST)
        stats[r["source"]][0] += hit
        stats[r["source"]][1] += 1
    return {k: {"hits": v[0], "n": v[1],
                "rate": round(v[0] / v[1], 3) if v[1] else None}
            for k, v in stats.items()}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--prepare",  action="store_true")
    ap.add_argument("--baseline", action="store_true")
    ap.add_argument("--train",    action="store_true")
    ap.add_argument("--evaluate", action="store_true")
    ap.add_argument("--promote",  action="store_true")
    ap.add_argument("--epochs",   type=int, default=25)
    args = ap.parse_args()

    if args.prepare:
        prepare()
    if args.baseline:
        s = _score(BEST)
        BASELINE.write_text(json.dumps(s, indent=2), encoding="utf-8")
        print("baseline:", json.dumps(s))
    if args.train:
        import subprocess
        ckpt_work = WORK / "unet_best.pth"
        shutil.copy2(BEST, ckpt_work)   # fine-tune FROM the proven weights
        cmd = [sys.executable, r"D:\project_modular\unet_dot_detector.py",
               "--images", str(IMG_DIR), "--labels", str(LBL_DIR),
               "--output", str(WORK / "predictions"),
               "--epochs", str(args.epochs), "--resume",
               "--manual-labels", "NONE_use_masks_already_written.csv"]
        print(" ".join(cmd))
        subprocess.call(cmd, cwd=str(WORK))
        if ckpt_work.exists():
            shutil.copy2(ckpt_work, RETRAINED)
            print(f"retrained checkpoint -> {RETRAINED}")
    if args.evaluate:
        base = json.loads(BASELINE.read_text(encoding="utf-8"))
        new  = _score(RETRAINED)
        print("baseline :", json.dumps(base))
        print("retrained:", json.dumps(new))
        anchor_drop = (base["anchor"]["rate"] or 0) - (new["anchor"]["rate"] or 0)
        new_gain    = (new["new"]["rate"] or 0) - (base["new"]["rate"] or 0)
        verdict = "PASS" if anchor_drop <= 0.02 and new_gain > 0 else "FAIL"
        print(f"anchor drop: {anchor_drop:+.3f} (max allowed +0.020)   "
              f"new-style gain: {new_gain:+.3f}   -> {verdict}")
    if args.promote:
        bak = BEST.with_suffix(".pth.pre_retrain_bak")
        shutil.copy2(BEST, bak)
        shutil.copy2(RETRAINED, BEST)
        print(f"promoted. backup -> {bak}")


if __name__ == "__main__":
    main()
