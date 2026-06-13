"""next_chunk.py -- write the next <=N not-done records of an index to a chunk
file. Exit 0 = work written, 3 = all done OR past deadline. Low-RAM, short-lived
(loads status once, exits) so a cmd loop can call it repeatedly without holding
memory while main.py runs the chunk.
"""
import argparse, csv, sys, time
from pathlib import Path

csv.field_size_limit(2_000_000)
OUT = Path(r"D:\project_outputs")
STAGES = ["latlong", "grid", "location", "county", "dot"]


def done_stems(shard):
    done = set()
    for fp in (OUT / "processing_status.csv", OUT / f"processing_status.{shard}.csv"):
        if not fp.exists():
            continue
        with fp.open(newline="", encoding="utf-8", errors="replace") as f:
            for r in csv.DictReader(f):
                s = r.get("pdf_stem", "")
                if not s:
                    continue
                st = [(r.get(f"{x}_status") or "") for x in STAGES]
                if any(st) and all(v in ("done", "failed", "skipped") for v in st):
                    done.add(s)
    return done


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--index", required=True)
    ap.add_argument("--shard", required=True)
    ap.add_argument("--chunk", type=int, default=50)
    ap.add_argument("--out", default=str(OUT / "_chunks" / "chunk.csv"))
    ap.add_argument("--deadline-seconds", type=int, default=17400)
    a = ap.parse_args()

    marker = OUT / f"_chain_start_{a.shard}.txt"
    if marker.exists():
        start = float(marker.read_text().strip() or time.time())
    else:
        start = time.time(); marker.write_text(str(start))
    if time.time() - start > a.deadline_seconds:
        print("DEADLINE"); sys.exit(3)

    with open(a.index, newline="", encoding="utf-8", errors="replace") as f:
        rd = csv.DictReader(f); cols = rd.fieldnames; rows = list(rd)
    done = done_stems(a.shard)
    todo = [r for r in rows if r.get("pdf_stem") not in done]
    print(f"{len(done)} done-overlap, {len(todo)} remaining of {len(rows)} "
          f"(elapsed {int((time.time()-start)/60)}m)")
    if not todo:
        sys.exit(3)
    part = todo[:a.chunk]
    outp = Path(a.out); outp.parent.mkdir(parents=True, exist_ok=True)
    with outp.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols); w.writeheader(); w.writerows(part)
    sys.exit(0)


if __name__ == "__main__":
    main()
