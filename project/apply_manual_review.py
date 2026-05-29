"""
apply_manual_review.py
======================
Reads user-filled review CSVs from D:/project_outputs_local/review/,
validates the entered PLSS data, then re-runs coordinate enrichment
using the corrected values.

Usage
-----
  python apply_manual_review.py                   # dry-run preview
  python apply_manual_review.py --apply           # actually update dot_coordinates.csv
  python apply_manual_review.py --apply --output D:\\project_outputs_local

After filling in the FILL_* columns in the review CSVs, run with --apply
to re-enrich and update the output.
"""

from __future__ import annotations

import argparse
import csv
import os
import re
import sys
from pathlib import Path

_DEFAULT_OUTPUT = r"D:\project_outputs_local"
_D_PROJECT      = Path(r"D:\project_modular\project")
if _D_PROJECT.exists() and str(_D_PROJECT) not in sys.path:
    sys.path.insert(0, str(_D_PROJECT))

_REVIEW_FILES = [
    "01_bounds_invalid.csv",
    "02_no_plss_address.csv",
    "03_no_section.csv",
    "04_approx_centroid_partial_plss.csv",   # no-dot records, section centroid only
]

# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

def _validate_section(v: str) -> str | None:
    """Return cleaned section string or None if invalid."""
    v = v.strip()
    if not v:
        return None
    try:
        n = int(float(v))
        if 1 <= n <= 36:
            return str(n)
    except (ValueError, TypeError):
        pass
    return None


def _validate_township(v: str) -> str | None:
    """Return cleaned township string or None if invalid."""
    v = v.strip().upper()
    if not v:
        return None
    # Accept formats: "25N", "25 N", "T25N", "25"
    v = re.sub(r"^T\s*", "", v)
    m = re.match(r"^(\d+)\s*([NS]?)$", v)
    if not m:
        return None
    num, d = int(m.group(1)), m.group(2)
    # Validate range
    if d == "S":
        if not (1 <= num <= 8):
            return None
    else:
        if not (1 <= num <= 29):
            return None
    return f"{num}{d}" if d else str(num)


def _validate_range(v: str) -> str | None:
    """Return cleaned range string or None if invalid."""
    v = v.strip().upper()
    if not v:
        return None
    # Accept: "13E", "13 E", "R13E", "13"
    v = re.sub(r"^R\s*", "", v)
    m = re.match(r"^(\d+)\s*([EW]?)$", v)
    if not m:
        return None
    num, d = int(m.group(1)), m.group(2)
    if d == "E":
        if not (1 <= num <= 26):
            return None
    elif d == "W":
        if not (1 <= num <= 28):
            return None
    else:
        if not (1 <= num <= 28):
            return None
    return f"{num}{d}" if d else str(num)


# ---------------------------------------------------------------------------
# Load corrections from filled review CSVs
# ---------------------------------------------------------------------------

def load_corrections(review_dir: Path) -> dict[str, dict]:
    """
    Returns {pdf_stem: {section, township, range}} for all confirmed rows
    with at least one filled FILL_* column.
    """
    corrections: dict[str, dict] = {}
    errors: list[str] = []

    for fname in _REVIEW_FILES:
        fpath = review_dir / fname
        if not fpath.exists():
            continue

        with fpath.open(newline="", encoding="utf-8-sig") as f:
            rows = list(csv.DictReader(f))

        n_confirmed = n_filled = n_errors = 0
        for row in rows:
            status = row.get("review_status", "").strip().lower()
            if status not in ("confirmed", "yes", "y", "ok", "done", "1", "true"):
                continue
            n_confirmed += 1

            stem = row.get("pdf_stem", "").strip()
            if not stem:
                continue

            # Parse each FILL_* column
            raw_sect = row.get("FILL_section",  "") or row.get("fill_section",  "")
            raw_twp  = row.get("FILL_township", "") or row.get("fill_township", "")
            raw_rng  = row.get("FILL_range",    "") or row.get("fill_range",    "")

            sect = _validate_section(raw_sect) if raw_sect.strip() else None
            twp  = _validate_township(raw_twp) if raw_twp.strip() else None
            rng  = _validate_range(raw_rng)    if raw_rng.strip() else None

            # Validate — collect field-level errors
            if raw_sect.strip() and sect is None:
                errors.append(f"  {fname}  {stem}  FILL_section={raw_sect!r} -> invalid (must be 1-36)")
                n_errors += 1
            if raw_twp.strip() and twp is None:
                errors.append(f"  {fname}  {stem}  FILL_township={raw_twp!r} -> invalid (e.g. 25N or 25)")
                n_errors += 1
            if raw_rng.strip() and rng is None:
                errors.append(f"  {fname}  {stem}  FILL_range={raw_rng!r} -> invalid (e.g. 13E or 13)")
                n_errors += 1

            if n_errors > 0 and (raw_sect.strip() or raw_twp.strip() or raw_rng.strip()):
                continue  # skip this row if any field had error

            # At least one filled field required
            if not any([sect, twp, rng]):
                continue

            n_filled += 1
            if stem in corrections:
                # Merge: later file takes precedence per-field
                if sect: corrections[stem]["section"]  = sect
                if twp:  corrections[stem]["township"] = twp
                if rng:  corrections[stem]["range"]    = rng
            else:
                corrections[stem] = {
                    k: v for k, v in
                    [("section", sect), ("township", twp), ("range", rng)]
                    if v is not None
                }

        print(f"  {fname}: {len(rows)} rows, {n_confirmed} confirmed, "
              f"{n_filled} with FILL_ data, {n_errors} validation errors")

    if errors:
        print(f"\n  *** VALIDATION ERRORS ({len(errors)}) — these rows will be SKIPPED:")
        for e in errors:
            print(e)
        print()

    return corrections


# ---------------------------------------------------------------------------
# Merge corrections into normalised_status rows
# ---------------------------------------------------------------------------

def _apply_corrections(norm_rows: list[dict], corrections: dict[str, dict]) -> tuple[list[dict], int]:
    """
    Apply PLSS corrections to normalised rows (in-place copy).
    Returns (patched_rows, n_patched).
    """
    patched = []
    n_patched = 0
    for r in norm_rows:
        stem = r.get("pdf_stem", "")
        corr = corrections.get(stem)
        if corr:
            r = dict(r)  # copy
            if "section" in corr:
                r["section"]              = corr["section"]
                r["location_section"]     = corr["section"]
            if "township" in corr:
                r["township"]             = corr["township"]
                r["location_township"]    = corr["township"]
            if "range" in corr:
                r["range"]                = corr["range"]
                r["location_range"]       = corr["range"]
            n_patched += 1
        patched.append(r)
    return patched, n_patched


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run(output_root: Path, apply: bool) -> None:
    review_dir = output_root / "review"

    if not any((review_dir / f).exists() for f in _REVIEW_FILES):
        print(f"No review files found in {review_dir}")
        print("Run setup_review_queue.py first, then fill in the FILL_* columns.")
        sys.exit(1)

    print(f"\n=== Loading corrections from {review_dir} ===\n")
    corrections = load_corrections(review_dir)

    if not corrections:
        print("No confirmed corrections found.")
        print("In the review CSVs, set review_status='confirmed' for rows you've filled in.")
        sys.exit(0)

    print(f"\n  Total corrections loaded: {len(corrections)}")
    # Summary by field
    n_sect = sum(1 for c in corrections.values() if "section" in c)
    n_twp  = sum(1 for c in corrections.values() if "township" in c)
    n_rng  = sum(1 for c in corrections.values() if "range" in c)
    print(f"    section  filled: {n_sect}")
    print(f"    township filled: {n_twp}")
    print(f"    range    filled: {n_rng}")

    # --- Load original processing status ------------------------------------
    status_csv = output_root / "processing_status.csv"
    if not status_csv.exists():
        print(f"ERROR: {status_csv} not found.")
        sys.exit(1)

    with status_csv.open(newline="", encoding="utf-8") as f:
        all_status = list(csv.DictReader(f))

    # --- Check which corrections refer to dot_done records ------------------
    dot_done_stems = {r["pdf_stem"] for r in all_status if r.get("dot_status") == "done"}
    already_resolved = set()
    coord_csv = output_root / "dot_coordinates.csv"
    if coord_csv.exists():
        with coord_csv.open(newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if row.get("resolved_lat"):
                    already_resolved.add(row["pdf_stem"])

    # dot_skipped records (G category) also eligible via section centroid
    dot_skipped_stems = {r["pdf_stem"] for r in all_status
                         if r.get("dot_status") == "skipped"}

    valid_corr = {s: c for s, c in corrections.items()
                  if (s in dot_done_stems or s in dot_skipped_stems)
                  and s not in already_resolved}
    already_done  = {s for s in corrections if s in already_resolved}
    not_eligible  = {s for s in corrections
                     if s not in dot_done_stems and s not in dot_skipped_stems}

    n_dot_skipped_corr = sum(1 for s in valid_corr if s in dot_skipped_stems)

    print(f"\n  Corrections applicable to unresolved records: {len(valid_corr)}")
    if n_dot_skipped_corr:
        print(f"    of which dot_skipped (centroid approx): {n_dot_skipped_corr}")
    if already_done:
        print(f"  Already resolved (will be skipped): {len(already_done)}")
    if not_eligible:
        print(f"  Stem not in dot_done/skipped (will be skipped): {len(not_eligible)}")

    if not valid_corr:
        print("\nNothing new to apply.")
        sys.exit(0)

    # --- Dry run: preview sample --------------------------------------------
    print(f"\n  Sample of corrections to apply:")
    for i, (stem, corr) in enumerate(list(valid_corr.items())[:8]):
        r = next((r for r in all_status if r["pdf_stem"] == stem), {})
        print(f"    {stem[:45]}")
        orig = (f"sect={r.get('location_section','?')!r} "
                f"twp={r.get('location_township','?')!r} "
                f"rng={r.get('location_range','?')!r}")
        patched_parts = []
        if "section"  in corr: patched_parts.append(f"sect={corr['section']!r}")
        if "township" in corr: patched_parts.append(f"twp={corr['township']!r}")
        if "range"    in corr: patched_parts.append(f"rng={corr['range']!r}")
        print(f"      orig:   {orig}")
        print(f"      patched: {' '.join(patched_parts)}")

    if not apply:
        print(f"\n  [DRY RUN] Add --apply to actually update dot_coordinates.csv")
        print(f"  Up to {len(valid_corr)} new records could be resolved.\n")
        return

    # --- Run enrichment with corrections ------------------------------------
    print(f"\n=== Applying {len(valid_corr)} corrections and re-running enrichment ===\n")

    try:
        from coord.coord_enricher import enrich_with_coordinates
        from run_coord_enrichment import _normalise_row, _load_review_decisions
    except ImportError as e:
        print(f"ERROR importing modules: {e}")
        sys.exit(1)

    # Load review decisions (human dot corrections)
    review_decisions = _load_review_decisions(output_root)

    # Build normalised rows for dot_done records (Passes 1-4 as usual)
    dot_done_rows = [r for r in all_status if r.get("dot_status") == "done"]

    # Apply manual PLSS corrections on top of status rows
    dot_done_rows_patched = []
    for r in dot_done_rows:
        corr = valid_corr.get(r["pdf_stem"])
        if corr:
            r = dict(r)
            if "section"  in corr: r["location_section"]  = corr["section"]
            if "township" in corr: r["location_township"] = corr["township"]
            if "range"    in corr: r["location_range"]    = corr["range"]
        dot_done_rows_patched.append(r)

    norm_rows = [_normalise_row(r, review_decisions.get(r["pdf_stem"]))
                 for r in dot_done_rows_patched]

    # Also include dot_skipped records that now have corrections (centroid approx)
    dot_skipped_with_corr = [
        r for r in all_status
        if r.get("dot_status") == "skipped" and r["pdf_stem"] in valid_corr
    ]
    for r in dot_skipped_with_corr:
        corr = valid_corr[r["pdf_stem"]]
        r = dict(r)
        if "section"  in corr: r["location_section"]  = corr["section"]
        if "township" in corr: r["location_township"] = corr["township"]
        if "range"    in corr: r["location_range"]    = corr["range"]
        nr = _normalise_row(r, review_decisions.get(r["pdf_stem"]))
        nr["dot_found"]          = "false"       # no dot position
        nr["centroid_candidate"] = "true"        # eligible for Pass 5 centroid
        norm_rows.append(nr)

    if dot_skipped_with_corr:
        print(f"  Added {len(dot_skipped_with_corr)} dot_skipped records "
              f"as centroid candidates (Pass 5)")

    # Write temp CSV
    tmp_dir = output_root / "_coord_enrich_tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    tmp_csv = tmp_dir / "normalised_with_corrections.csv"

    seen: dict[str, None] = {}
    for row in norm_rows:
        seen.update(dict.fromkeys(row.keys()))
    with tmp_csv.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(seen), extrasaction="ignore")
        w.writeheader()
        w.writerows(norm_rows)

    output_csv = output_root / "dot_coordinates.csv"
    resolved = enrich_with_coordinates(
        success_csv            = tmp_csv,
        output_csv             = output_csv,
        county_constraints_dir = output_root,
        status_csv             = status_csv,
    )

    # Cleanup temp
    tmp_csv.unlink(missing_ok=True)
    try:
        tmp_dir.rmdir()
    except OSError:
        pass

    # Summary
    total_dot_done = len(dot_done_rows)
    pct = resolved / total_dot_done * 100 if total_dot_done else 0
    print(f"\n{'='*60}")
    print(f"  Resolved: {resolved:,} / {total_dot_done:,} ({pct:.1f}%)")
    print(f"  Output  : {output_csv}")
    failures = output_root / "coord_resolution_failures.csv"
    if failures.exists():
        with failures.open(encoding="utf-8") as f:
            nf = sum(1 for _ in csv.DictReader(f))
        print(f"  Failures: {nf}")
    print(f"{'='*60}\n")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(
        description="Apply manual PLSS corrections and re-run enrichment"
    )
    ap.add_argument("--output", default=_DEFAULT_OUTPUT,
                    help=f"Pipeline output root (default: {_DEFAULT_OUTPUT})")
    ap.add_argument("--apply", action="store_true",
                    help="Actually update dot_coordinates.csv (default: dry-run preview only)")
    args = ap.parse_args()

    run(Path(args.output), apply=args.apply)


if __name__ == "__main__":
    main()
