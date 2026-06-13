"""Upload source PDFs present on D: but missing from S3, to their canonical
keys: pdfs/ExportedFolderContents_{N}/{year}/{month}/{stem}.pdf  (the key the
website builds). Source path comes from dataset_index.pdf_path (exact on-disk
location, incl. nested-month folders). Usage: python upload_missing_s3.py [--apply]
"""
import argparse, csv, os, re
from pathlib import Path

csv.field_size_limit(2_000_000)
OUT = Path(r"D:\project_outputs")
BKT = os.environ.get("S3_BUCKET", "osu-well-records-225989338968")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--collection", default="", help="limit to collection number")
    a = ap.parse_args()

    # stem -> real disk path
    path = {}
    with (OUT / "dataset_index.csv").open(newline="", encoding="utf-8", errors="replace") as f:
        for r in csv.DictReader(f):
            path[r.get("pdf_stem", "")] = r.get("pdf_path", "")

    todo = []
    with (OUT / "s3_missing_upload.csv").open(newline="", encoding="utf-8", errors="replace") as f:
        for r in csv.DictReader(f):
            c = r["collection_num"]
            if a.collection and c != a.collection:
                continue
            stem = r["pdf_stem"]
            src = path.get(stem, "")
            if not src or not os.path.exists(src):
                print(f"  MISSING ON DISK: {stem}")
                continue
            key = f"pdfs/ExportedFolderContents_{c}/{r['year']}/{r['month']}/{stem}.pdf"
            todo.append((src, key))
    print(f"{len(todo)} files to upload")
    if not a.apply:
        for s, k in todo[:5]:
            print(f"  {k}")
        print("Dry run — use --apply.")
        return
    import boto3
    cli = boto3.client("s3")
    ok = 0
    for src, key in todo:
        try:
            cli.upload_file(src, BKT, key)
            ok += 1
            if ok % 50 == 0:
                print(f"  {ok}/{len(todo)}")
        except Exception as exc:
            print(f"  FAIL {key}: {exc}")
    print(f"uploaded {ok}/{len(todo)}")


if __name__ == "__main__":
    main()
