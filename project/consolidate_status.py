"""
consolidate_status.py -- fold status shards back into the base CSV.

Sharded saves (STATUS_SHARD_SUFFIX) give each run its own small status
file so concurrent runs never contend for one lock. After runs finish,
this folds every processing_status.<suffix>.csv into the base file
(shard rows override base; newer shard mtime wins on conflicts) and
deletes the shards.

Refuses to run while python pipeline processes are alive.

Usage:  python consolidate_status.py [--output D:\\project_outputs]
"""
import argparse
import csv
import os
import subprocess
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
csv.field_size_limit(2_000_000)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--output", default=r"D:\project_outputs")
    ap.add_argument("--force", action="store_true",
                    help="skip the running-process check")
    args = ap.parse_args()
    base = Path(args.output) / "processing_status.csv"

    if not args.force:
        out = subprocess.run(
            ["powershell", "-Command",
             "(Get-Process python -ErrorAction SilentlyContinue | "
             "Measure-Object).Count"],
            capture_output=True, text=True)
        n = int((out.stdout or "0").strip() or 0)
        # 1 = this very script
        if n > 1:
            print(f"{n} python processes running — refusing to consolidate "
                  "(use --force to override)")
            sys.exit(1)

    shards = sorted(
        (p for p in base.parent.glob(base.stem + ".*" + base.suffix)
         if p != base and not p.name.endswith(".new")),
        key=lambda p: p.stat().st_mtime)
    if not shards:
        print("no shards to consolidate")
        return

    rows: dict[str, dict] = {}
    fieldnames: list[str] | None = None
    for src in [base] + shards if base.exists() else shards:
        with src.open(newline="", encoding="utf-8", errors="replace") as f:
            reader = csv.DictReader(f)
            if fieldnames is None:
                fieldnames = reader.fieldnames
            for r in reader:
                if any(len(v or "") > 10_000 for v in r.values()):
                    continue
                rows[r["pdf_stem"]] = {k: r.get(k, "") for k in fieldnames}
        print(f"  merged {src.name}")

    tmp = base.with_suffix(".csv.new")
    with tmp.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows.values())
    for attempt in range(8):
        try:
            os.replace(tmp, base)
            break
        except PermissionError:
            time.sleep(4 * (attempt + 1))
    print(f"consolidated {len(rows):,} rows -> {base}")
    for s in shards:
        s.unlink(missing_ok=True)
        s.with_suffix(".csv.lock").unlink(missing_ok=True)
    print(f"removed {len(shards)} shard(s)")


if __name__ == "__main__":
    main()
