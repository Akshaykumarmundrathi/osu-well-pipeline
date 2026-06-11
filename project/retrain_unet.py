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
NEW_CSV   = Path(r"D:\project_outputs\dot_labels\manual_labels.csv")
BEST      = Path(r"D:\project_modular\unet_best.pth")
RETRAINED = WORK / "unet_retrained.pth"
HOLDOUT   = WORK / "holdout.csv"
BASELINE  = WORK / "baseline.json"
SEED      = 42
HOLDOUT_FRAC = 0.15
HIT_DIST  = 0.06


def _load_old() -> list[dict]:
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
                        "source": "early", "negative": False})
    return out


def _load_new() -> list[dict]:
    out = []
    if not NEW_CSV.exists():
        return out
    from PIL import Image
    for r in csv.DictReader(NEW_CSV.open(newline="", encoding="utf-8")):
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
    rows = _load_old() + _load_new()
    print(f"training pool: {sum(1 for r in rows if r['source']=='early')} early "
          f"+ {sum(1 for r in rows if r['source']=='new')} new")
    rng = random.Random(SEED)
    holdout, train = [], []
    for src in ("early", "new"):
        grp = [r for r in rows if r["source"] == src]
        rng.shuffle(grp)
        k = max(1, int(len(grp) * HOLDOUT_FRAC)) if grp else 0
        holdout += grp[:k]
        train   += grp[k:]

    for d in (IMG_DIR, LBL_DIR):
        if d.exists():
            shutil.rmtree(d)
        d.mkdir(parents=True)
    from PIL import Image
    n_img = 0
    for r in train:
        src = Path(r["image_path"])
        dst = IMG_DIR / f"{src.parent.name[:40]}_{src.name}"
        shutil.copy2(src, dst)
        if r["negative"]:
            write_empty_mask(dst, LBL_DIR)
        else:
            with Image.open(dst) as im:
                w, h = im.size
            write_manual_mask(dst, LBL_DIR,
                              [(int(r["x_norm"] * w), int(r["y_norm"] * h))])
        n_img += 1
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
    stats = {"early": [0, 0], "new": [0, 0]}
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
        early_drop = (base["early"]["rate"] or 0) - (new["early"]["rate"] or 0)
        new_gain   = (new["new"]["rate"] or 0) - (base["new"]["rate"] or 0)
        verdict = "PASS" if early_drop <= 0.02 and new_gain > 0 else "FAIL"
        print(f"early drop: {early_drop:+.3f} (max allowed +0.020)   "
              f"new-style gain: {new_gain:+.3f}   -> {verdict}")
    if args.promote:
        bak = BEST.with_suffix(".pth.pre_retrain_bak")
        shutil.copy2(BEST, bak)
        shutil.copy2(RETRAINED, BEST)
        print(f"promoted. backup -> {bak}")


if __name__ == "__main__":
    main()
