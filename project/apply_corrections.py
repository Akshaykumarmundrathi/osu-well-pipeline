"""
apply_corrections.py -- ingest map verify-panel corrections from GitHub issues
===============================================================================

The live map's "Verify / Fix" panel files structured GitHub issues
(label: correction). This script:

  1. pulls open correction issues via `gh`
  2. parses the field changes (section/township/range/county/quadrant)
  3. applies them to dot_coordinates.csv (authoritative map source)
  4. re-resolves coordinates through the PLSS database for wells whose
     STR fields changed (county/quadrant-only fixes keep coordinates)
  5. records every applied fix in corrections_applied.csv (audit trail)
  6. closes the issues with a comment
  7. leaves map rebuild/push to build_map_data.py (run after this)

VERIFIED-CORRECT issues are logged (confidence signal) and closed.

Usage:
    python apply_corrections.py            # dry run -- show pending fixes
    python apply_corrections.py --apply    # apply + close issues
Then:
    python build_map_data.py --output D:\\project_outputs --repo D:\\project_modular
    git add docs/data/well_locations.json && git commit && git push
"""
import argparse
import csv
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
csv.field_size_limit(2_000_000)

REPO     = "Akshaykumarmundrathi/osu-well-pipeline"
DOT_CSV  = Path(r"D:\project_outputs\dot_coordinates.csv")
AUDIT    = Path(r"D:\project_outputs\corrections_applied.csv")

_FIELD_RE = re.compile(r"^- (section|township|range|county|quadrant): `([^`]*)` -> `([^`]*)`",
                       re.M)
_STEM_RE  = re.compile(r"\*\*Well:\*\* `([^`]+)`")


def _gh_token() -> str:
    env = Path(r"D:\project_modular\.env")
    for line in env.read_text(encoding="utf-8").splitlines():
        if line.startswith("GITHUB_TOKEN="):
            return line.split("=", 1)[1].strip()
    return ""


def _api(path: str, method: str = "GET", payload: dict | None = None) -> object:
    import urllib.request
    req = urllib.request.Request(
        f"https://api.github.com{path}", method=method,
        data=json.dumps(payload).encode() if payload else None,
        headers={"Accept": "application/vnd.github+json",
                 "Authorization": f"Bearer {_gh_token()}",
                 "User-Agent": "osu-well-pipeline"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode() or "null")


def fetch_issues() -> list[dict]:
    """Open issues that look like verify-panel submissions.

    Matched by TITLE prefix ([CORRECTION] / [VERIFIED-CORRECT]) rather than
    label — GitHub silently drops URL-prefilled labels that don't exist in
    the repo, and non-collaborator submitters can't apply labels anyway.
    """
    try:
        issues = _api(f"/repos/{REPO}/issues?state=open&per_page=100")
    except Exception as exc:
        print("GitHub API failed:", str(exc)[:200])
        return []
    out = []
    for i in issues:
        if "pull_request" in i:
            continue
        title = i.get("title", "")
        labels = {l["name"] for l in i.get("labels", [])}
        if ("correction" in labels
                or title.startswith("[CORRECTION]")
                or title.startswith("[VERIFIED-CORRECT]")):
            out.append({"number": i["number"], "title": title,
                        "body": i.get("body", "")})
    return out


def parse_issue(issue: dict) -> dict | None:
    body = issue.get("body") or ""
    m = _STEM_RE.search(body)
    if not m:
        return None
    changes = {f: new for f, old, new in _FIELD_RE.findall(body)}
    return {"number": issue["number"], "stem": m.group(1),
            "verified": "VERIFIED-CORRECT" in issue.get("title", ""),
            "changes": changes}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    parsed = [p for p in (parse_issue(i) for i in fetch_issues()) if p]
    fixes    = [p for p in parsed if p["changes"] and not p["verified"]]
    verified = [p for p in parsed if p["verified"] or not p["changes"]]
    print(f"{len(fixes)} corrections, {len(verified)} verified-correct")
    for p in fixes:
        print(f"  #{p['number']} {p['stem'][:45]}: "
              + ", ".join(f"{k}->{v}" for k, v in p["changes"].items()))
    if not args.apply:
        print("\nDry run — use --apply to write.")
        return

    # Apply to dot_coordinates.csv
    with DOT_CSV.open(newline="", encoding="utf-8", errors="replace") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames
        rows = list(reader)
    by_stem = {p["stem"]: p for p in fixes}
    field_map = {"section": "section", "township": "township",
                 "range": "range", "county": "county_name",
                 "quadrant": "unet_nw"}
    audit_rows, applied = [], set()
    str_changed = set()
    for row in rows:
        p = by_stem.get(row.get("pdf_stem", ""))
        if not p:
            continue
        for f, newv in p["changes"].items():
            col = field_map.get(f)
            if col and col in row and row[col] != newv:
                audit_rows.append({"pdf_stem": row["pdf_stem"], "field": col,
                                   "old": row[col], "new": newv,
                                   "issue": p["number"]})
                row[col] = newv
                if f in ("section", "township", "range", "quadrant"):
                    str_changed.add(row["pdf_stem"])
        applied.add(p["number"])

    shutil.copy2(DOT_CSV, DOT_CSV.with_suffix(".csv.corr_bak"))
    tmp = DOT_CSV.with_suffix(".csv.new")
    with tmp.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader(); w.writerows(rows)
    tmp.replace(DOT_CSV)
    print(f"{len(audit_rows)} field fixes applied to {DOT_CSV}")

    if str_changed:
        print(f"NOTE: {len(str_changed)} wells changed STR/quadrant — their "
              f"coordinates need re-resolution:")
        print("  python run_coord_enrichment.py --output D:\\project_outputs "
              "--all-dot-done --include-centroid")

    # Audit trail
    new_file = not AUDIT.exists()
    with AUDIT.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["pdf_stem", "field", "old", "new", "issue"])
        if new_file:
            w.writeheader()
        w.writerows(audit_rows)

    # Close handled issues
    for p in parsed:
        if p["number"] in applied or p["verified"] or not p["changes"]:
            msg = ("Applied to the dataset — the map will update on the next publish. Thanks!"
                   if p["number"] in applied else
                   "Recorded as verified-correct. Thanks for checking!")
            try:
                _api(f"/repos/{REPO}/issues/{p['number']}/comments",
                     "POST", {"body": msg})
                _api(f"/repos/{REPO}/issues/{p['number']}",
                     "PATCH", {"state": "closed"})
            except Exception as exc:
                print(f"  close #{p['number']} failed: {str(exc)[:80]}")
    print("issues closed.")


if __name__ == "__main__":
    main()
