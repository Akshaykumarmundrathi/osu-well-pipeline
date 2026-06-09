"""
audit_c2345.py -- quality audit of current C2-C5 processing_status.csv

Produces:
  - Stage success rates and confidence distributions
  - Grid method breakdown
  - Location partial-extraction analysis (which field is missing)
  - County failure type breakdown
  - Year-by-year success trend
  - STR value sanity check (section 1-36, township/range plausibility)
  - Dot detection rate
  - Fully resolved records
"""
import csv
import re
import sys
import collections
from pathlib import Path

# Force UTF-8 output so box-drawing chars don't crash on cp1252 terminals
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

csv.field_size_limit(2_000_000)
P   = Path(r"D:\project_outputs\processing_status.csv")
IDX = Path(r"D:\project_outputs_c2345\dataset_index.csv")

# Load index stems so we only audit C2-C5 sample records
idx_stems: set[str] = set()
if IDX.exists():
    with IDX.open(newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            idx_stems.add(r["pdf_stem"])

rows = []
with P.open(newline="", encoding="utf-8", errors="replace") as f:
    for r in csv.DictReader(f):
        if r["pdf_stem"] in idx_stems:
            rows.append(r)

total   = len(rows)
started = [r for r in rows if r.get("grid_status") not in ("", "pending")]
N       = len(started)

SEP = "=" * 62
sep = "-" * 62

print(f"\n{SEP}")
print(f"  C2-C5 Quality Audit  --  {N} processed / {total} in sample")
print(f"{SEP}")

# ── 1. Stage success rates ────────────────────────────────────
print("\n[1] Stage success rates")
print(sep)
for stage in ("grid", "location", "county", "dot"):
    d  = sum(1 for r in started if r.get(f"{stage}_status") == "done")
    f_ = sum(1 for r in started if r.get(f"{stage}_status") == "failed")
    s  = sum(1 for r in started if r.get(f"{stage}_status") == "skipped")
    tot = d + f_
    rate = d * 100 // tot if tot else 0
    bar  = "#" * (rate // 5) + "." * (20 - rate // 5)
    print(f"  {stage:<10}  [{bar}] {rate:>3}%   done={d:>4}  failed={f_:>4}  skipped={s:>3}")

# ── 2. Grid method breakdown ──────────────────────────────────
print(f"\n[2] Grid detection methods")
print(sep)
methods = collections.Counter(
    r.get("grid_method", "") for r in started if r.get("grid_status") == "done"
)
for m, c in methods.most_common():
    print(f"  {m:<32} {c:>4}")

# ── 3. Location confidence distribution ──────────────────────
print(f"\n[3] Location confidence distribution")
print(sep)
loc_done = [r for r in started if r.get("location_status") == "done"]
conf_bins = collections.Counter(
    int(r.get("location_confidence", "0") or 0) for r in loc_done
)
labels = {100: "100%  all 3 fields (sec+twp+rng)",
           66:  " 66%  2 of 3 fields",
           33:  " 33%  1 of 3 fields"}
for c in sorted(conf_bins, reverse=True):
    lbl = labels.get(c, f"{c:>3}%")
    print(f"  {lbl:<38} {conf_bins[c]:>4}")

# ── 4. Which field is missing in partial extractions ─────────
print(f"\n[4] Partial location -- missing field breakdown")
print(sep)
missing_sec = sum(1 for r in loc_done if not r.get("location_section",  "").strip())
missing_twp = sum(1 for r in loc_done if not r.get("location_township", "").strip())
missing_rng = sum(1 for r in loc_done if not r.get("location_range",    "").strip())
print(f"  Missing section    {missing_sec:>4}")
print(f"  Missing township   {missing_twp:>4}")
print(f"  Missing range      {missing_rng:>4}")
print(f"  (total done rows   {len(loc_done):>4})")

# ── 5. STR sanity check ───────────────────────────────────────
print(f"\n[5] STR value sanity check")
print(sep)
bad_sec = bad_twp = good = 0
bad_sec_examples = []
for r in loc_done:
    sec = r.get("location_section", "").strip()
    twp = r.get("location_township", "").strip()
    ok  = True
    if sec:
        try:
            sv = int(re.sub(r"[^0-9]", "", sec) or "0")
            if not (1 <= sv <= 36):
                bad_sec += 1
                ok = False
                if len(bad_sec_examples) < 3:
                    bad_sec_examples.append(f"sec={sec!r}")
        except Exception:
            bad_sec += 1; ok = False
    if twp:
        tv_str = re.sub(r"[^0-9]", "", twp)
        if tv_str:
            try:
                tv = int(tv_str)
                if not (1 <= tv <= 35):
                    bad_twp += 1; ok = False
            except Exception:
                bad_twp += 1; ok = False
    if ok:
        good += 1

print(f"  Plausible (sec 1-36, twp 1-35)  {good:>4}  "
      f"({good * 100 // len(loc_done) if loc_done else 0}%)")
print(f"  Bad / out-of-range section       {bad_sec:>4}"
      + (f"  e.g. {', '.join(bad_sec_examples)}" if bad_sec_examples else ""))
print(f"  Suspicious township              {bad_twp:>4}")

# ── 6. County error type breakdown ───────────────────────────
print(f"\n[6] County failure types")
print(sep)
ctypes = collections.Counter(
    r.get("county_error_type", "") for r in started if r.get("county_status") == "failed"
)
for t, c in ctypes.most_common():
    print(f"  {t or '(none)':<32} {c:>4}")

# ── 7. County confidence of successes ────────────────────────
print(f"\n[7] County name match score (done records)")
print(sep)
cscores = collections.Counter()
for r in started:
    if r.get("county_status") == "done":
        sc = int(r.get("county_score", "0") or 0)
        cscores[(sc // 10) * 10] += 1
for b in sorted(cscores, reverse=True):
    print(f"  {b:>3}-{b+9}%   {cscores[b]:>4}")

# ── 8. Year-by-year location success ─────────────────────────
print(f"\n[8] Year-by-year location success rate")
print(sep)
by_year: dict[str, list[int]] = collections.defaultdict(lambda: [0, 0])
for r in started:
    yr = r.get("year", "?")
    ls = r.get("location_status", "")
    if   ls == "done":   by_year[yr][0] += 1
    elif ls == "failed": by_year[yr][1] += 1
for yr in sorted(by_year):
    d, f_ = by_year[yr]
    tot  = d + f_
    rate = d * 100 // tot if tot else 0
    bar  = "#" * (rate // 5)
    print(f"  {yr}  {rate:>3}%  {bar:<20}  {d:>3}/{tot}")

# ── 9. Dot detection (of grid-success records) ────────────────
print(f"\n[9] Dot detection rate (grid-success records only)")
print(sep)
grid_done  = [r for r in started if r.get("grid_status") == "done"]
dot_done_n = sum(1 for r in grid_done if r.get("dot_status") == "done")
dot_fail_n = sum(1 for r in grid_done if r.get("dot_status") == "failed")
gn = len(grid_done)
print(f"  Grid detected      {gn:>4}")
print(f"  Dot found          {dot_done_n:>4}  ({dot_done_n * 100 // gn if gn else 0}%)")
print(f"  Dot not found      {dot_fail_n:>4}  ({dot_fail_n * 100 // gn if gn else 0}%)")

# ── 10. Fully resolved records ────────────────────────────────
print(f"\n[10] Fully resolved (location + county + dot all done)")
print(sep)
full = sum(
    1 for r in started
    if r.get("location_status") == "done"
    and r.get("county_status")  == "done"
    and r.get("dot_status")     == "done"
)
loc_cty = sum(
    1 for r in started
    if r.get("location_status") == "done"
    and r.get("county_status")  == "done"
)
print(f"  Location + county + dot    {full:>4}  ({full * 100 // N if N else 0}%)")
print(f"  Location + county only     {loc_cty:>4}  ({loc_cty * 100 // N if N else 0}%)")

# ── 11. Top grid form types ───────────────────────────────────
print(f"\n[11] Grid form types detected")
print(sep)
ftypes = collections.Counter(
    r.get("grid_form_type", "") for r in started if r.get("grid_status") == "done"
)
for t, c in ftypes.most_common():
    print(f"  {t or '(none)':<30} {c:>4}")

print(f"\n{SEP}\n")
