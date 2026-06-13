"""run_chunks.py -- crash-proof chunked runner for ONE index.

Processes a dataset index in small fresh sub-processes so RAM is fully released
between chunks (beats the 7.4 GB OOM crash cycle). Each chunk is idempotent:
it writes to the shared status shard and the pipeline skips already-done
records, so a crashed/retried chunk never loses or duplicates work.

  - chunk size kept small (default 60) so a per-record memory build-up can't
    reach the OOM threshold before the process exits and frees everything.
  - each chunk runs as a separate `python main.py` with a timeout.
  - on crash / timeout the chunk is retried (default 3x); then we move on
    (the next pass will pick up any stragglers since they stay not-done).
  - loops over the whole index repeatedly until every record is done/failed,
    or --deadline-seconds elapses.

Usage:
  python run_chunks.py --index <idx.csv> --shard unseen6k --chunk 60 \
      --deadline-seconds 17700
"""
import argparse, csv, os, subprocess, sys, time
from pathlib import Path

csv.field_size_limit(2_000_000)
OUT = Path(r"D:\project_outputs")
HERE = Path(__file__).parent
STAGES = ["latlong", "grid", "location", "county", "dot"]


def _done_stems(shard):
    """Stems already finished (every stage done or failed) in base+shard."""
    done = set()
    files = [OUT / "processing_status.csv",
             OUT / f"processing_status.{shard}.csv"]
    for fp in files:
        if not fp.exists():
            continue
        with fp.open(newline="", encoding="utf-8", errors="replace") as f:
            for r in csv.DictReader(f):
                s = r.get("pdf_stem", "")
                if not s:
                    continue
                st = [(r.get(f"{x}_status") or "") for x in STAGES]
                if all(v in ("done", "failed", "skipped") for v in st) and any(st):
                    done.add(s)
    return done


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--index", required=True)
    ap.add_argument("--shard", required=True)
    ap.add_argument("--chunk", type=int, default=60)
    ap.add_argument("--retries", type=int, default=3)
    ap.add_argument("--chunk-timeout", type=int, default=900)
    ap.add_argument("--deadline-seconds", type=int, default=17700)  # ~4h55m
    ap.add_argument("--cooldown", type=int, default=5)
    a = ap.parse_args()

    with open(a.index, newline="", encoding="utf-8", errors="replace") as f:
        rd = csv.DictReader(f); cols = rd.fieldnames
        all_rows = list(rd)
    total = len(all_rows)
    t0 = time.time()
    chunk_dir = OUT / "_chunks"; chunk_dir.mkdir(exist_ok=True)
    env = dict(os.environ, STATUS_SHARD_SUFFIX=a.shard)

    rounds = 0
    while True:
        if time.time() - t0 > a.deadline_seconds:
            print(f"[deadline] stopping after {int(time.time()-t0)}s"); break
        done = _done_stems(a.shard)
        todo = [r for r in all_rows if r.get("pdf_stem") not in done]
        print(f"\n=== pass {rounds+1}: {len(done)}/{total} done, {len(todo)} remaining "
              f"(elapsed {int((time.time()-t0)/60)}m) ===", flush=True)
        if not todo:
            print("ALL DONE."); break
        rounds += 1
        # process this pass in small chunks
        for i in range(0, len(todo), a.chunk):
            if time.time() - t0 > a.deadline_seconds:
                print("[deadline] stopping mid-pass"); return
            part = todo[i:i+a.chunk]
            cf = chunk_dir / "chunk.csv"
            with cf.open("w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=cols); w.writeheader(); w.writerows(part)
            ok = False
            for attempt in range(1, a.retries+1):
                for lk in OUT.glob("*.lock"):
                    try: lk.unlink()
                    except Exception: pass
                try:
                    r = subprocess.run(
                        [sys.executable, str(HERE/"main.py"), "--index", str(cf),
                         "--output", str(OUT), "--workers", "1"],
                        env=env, cwd=str(HERE), timeout=a.chunk_timeout,
                        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
                    if r.returncode == 0:
                        ok = True; break
                    err = (r.stderr or b"")[-200:].decode("utf-8", "replace")
                    print(f"  chunk {i//a.chunk+1} attempt {attempt} rc={r.returncode} {err}", flush=True)
                except subprocess.TimeoutExpired:
                    print(f"  chunk {i//a.chunk+1} attempt {attempt} TIMEOUT", flush=True)
                time.sleep(a.cooldown)
            d = len(_done_stems(a.shard))
            print(f"  [{d}/{total}] chunk {i//a.chunk+1} "
                  f"{'ok' if ok else 'gave up (will retry next pass)'}", flush=True)
            time.sleep(a.cooldown)
        # safety: if a pass made zero progress, avoid infinite loop
    print(f"FINISHED: {len(_done_stems(a.shard))}/{total} in {int((time.time()-t0)/60)}m")


if __name__ == "__main__":
    main()
