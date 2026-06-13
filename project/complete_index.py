"""complete_index.py -- append every source PDF on disk that is missing from
dataset_index.csv, so the index is the true universe. Idempotent, additive
(never rewrites or reorders existing rows). Backs up first.

Source layout: D:\\ExportedFolderContents (N)\\<year>\\<month>\\<stem>.pdf
Usage: python complete_index.py [--apply]
"""
import argparse, csv, os, re, shutil, time
from pathlib import Path

csv.field_size_limit(2_000_000)
INDEX = Path(r"D:\project_outputs\dataset_index.csv")
SRC   = Path("D:/")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()

    with INDEX.open(newline="", encoding="utf-8", errors="replace") as f:
        rd = csv.DictReader(f); cols = rd.fieldnames
        have = {r.get("pdf_stem", "") for r in rd}
    print(f"index has {len(have):,} stems")

    new = []
    for d in sorted(SRC.glob("ExportedFolderContents (*)")):
        m = re.search(r"\((\d+)\)", d.name)
        cnum = m.group(1) if m else ""
        coll = d.name                      # "ExportedFolderContents (N)"
        for root, _, files in os.walk(d):
            rp = Path(root)
            parts = rp.relative_to(d).parts        # (year, month) usually
            year = parts[0] if len(parts) >= 1 else ""
            month = parts[1] if len(parts) >= 2 else ""
            for fn in files:
                if not fn.lower().endswith(".pdf"):
                    continue
                stem = fn[:-4]
                if stem in have:
                    continue
                have.add(stem)
                full = str(rp / fn)
                row = {c: "" for c in cols}
                row.update({
                    "pdf_stem": stem, "pdf_path": full, "zip_path": "",
                    "collection": coll, "collection_num": cnum,
                    "year": year, "month": month,
                    "collection_safe": coll.replace(" ", "_").replace("(", "").replace(")", ""),
                    "month_safe": month.replace(" ", "_"),
                    "file_size_bytes": str(os.path.getsize(full)) if os.path.exists(full) else "",
                    "scan_timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
                    "internal_path": "",
                })
                new.append({k: row.get(k, "") for k in cols})
    print(f"missing-from-index PDFs found on disk: {len(new):,}")
    by_coll = {}
    for r in new:
        by_coll[r["collection_num"]] = by_coll.get(r["collection_num"], 0) + 1
    for c in sorted(by_coll, key=lambda x: int(x or 0)):
        print(f"  C{c}: +{by_coll[c]:,}")
    if not a.apply:
        print("\nDry run — use --apply to append.")
        return
    shutil.copy2(INDEX, INDEX.with_suffix(".csv.precomplete_bak"))
    with INDEX.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writerows(new)
    print(f"appended {len(new):,} rows -> {INDEX}")


if __name__ == "__main__":
    main()
