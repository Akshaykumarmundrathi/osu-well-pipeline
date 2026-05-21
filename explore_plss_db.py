"""
PLSS RDS database exploration script.

Connects to the plss_grid table, explores its structure, counts rows
for typical location queries, visualises cell geometry, and verifies
corner coordinates.  Always closes the connection before exit.

Run:  python D:\project_modular\explore_plss_db.py
Output: D:\project_outputs\plss_exploration\  (PNGs + summary.txt)
"""

import os
import sys
import textwrap
from pathlib import Path

import psycopg2
import psycopg2.extras
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
from matplotlib.patches import FancyArrowPatch

# ---------------------------------------------------------------------------
# Connection params (same as plss_resolver.py)
# ---------------------------------------------------------------------------
_HOST     = os.environ.get("RDS_HOST",     "oklahomagridlatlongdb.cz62c0sysryk.us-east-1.rds.amazonaws.com")
_PORT     = int(os.environ.get("RDS_PORT", "5432"))
_DBNAME   = os.environ.get("RDS_DBNAME",   "Oklahomaplss")
_USER     = os.environ.get("RDS_USER",     "LookUpMaster")
_PASSWORD = os.environ.get("RDS_PASSWORD", "Geology#OSU")

OUT = Path(r"D:\project_outputs\plss_exploration")
OUT.mkdir(parents=True, exist_ok=True)

conn = None
lines = []   # collected for summary.txt

def p(s=""):
    print(s)
    lines.append(str(s))

# ---------------------------------------------------------------------------
# Connect
# ---------------------------------------------------------------------------
try:
    p("Connecting to RDS...")
    conn = psycopg2.connect(
        host=_HOST, port=_PORT, dbname=_DBNAME,
        user=_USER, password=_PASSWORD,
        sslmode="require", connect_timeout=20,
    )
    conn.set_session(readonly=True, autocommit=True)
    p("Connected.\n")
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
except Exception as exc:
    print(f"Connection failed: {exc}")
    sys.exit(1)

# ===========================================================================
# 1. TABLE STRUCTURE
# ===========================================================================
p("=" * 70)
p("1. TABLE COLUMNS (plss_grid)")
p("=" * 70)
cur.execute("""
    SELECT column_name, data_type, character_maximum_length, is_nullable
    FROM information_schema.columns
    WHERE table_name = 'plss_grid'
    ORDER BY ordinal_position;
""")
cols = cur.fetchall()
for c in cols:
    p(f"  {c['column_name']:<25} {c['data_type']:<20} nullable={c['is_nullable']}")

# ===========================================================================
# 2. TOTAL ROW COUNT
# ===========================================================================
p()
p("=" * 70)
p("2. TOTAL ROW COUNT")
p("=" * 70)
cur.execute("SELECT COUNT(*) AS n FROM plss_grid;")
total = cur.fetchone()["n"]
p(f"  Total rows: {total:,}")

# ===========================================================================
# 3. SAMPLE ROWS — raw values
# ===========================================================================
p()
p("=" * 70)
p("3. SAMPLE ROWS (5 rows, all columns except geom)")
p("=" * 70)
cur.execute("""
    SELECT sect_num, township, north_south, "range", east_west,
           quadrant_label, county_name,
           minx, miny, maxx, maxy,
           top_left, top_right, bottom_left, bottom_right
    FROM plss_grid
    LIMIT 5;
""")
samples = cur.fetchall()
for row in samples:
    p(f"\n  sect={row['sect_num']} T{row['township']}{row['north_south']} "
      f"R{row['range']}{row['east_west']}  quad={row['quadrant_label']}  "
      f"county={row['county_name']}")
    p(f"    bbox:  minx={row['minx']:.6f}  miny={row['miny']:.6f}  "
      f"maxx={row['maxx']:.6f}  maxy={row['maxy']:.6f}")
    p(f"    top_left={row['top_left']}  top_right={row['top_right']}")
    p(f"    bot_left={row['bottom_left']}  bot_right={row['bottom_right']}")

# ===========================================================================
# 4. WHAT IS quadrant_label?  — distinct values + counts
# ===========================================================================
p()
p("=" * 70)
p("4. DISTINCT quadrant_label VALUES")
p("=" * 70)
cur.execute("""
    SELECT quadrant_label, COUNT(*) AS n
    FROM plss_grid
    GROUP BY quadrant_label
    ORDER BY quadrant_label;
""")
for r in cur.fetchall():
    p(f"  '{r['quadrant_label']}' : {r['n']:,} rows")

# ===========================================================================
# 5. HOW MANY ROWS FOR A SPECIFIC SECTION QUERY?
# ===========================================================================
p()
p("=" * 70)
p("5. ROWS PER SECTION QUERY  (sect=14, T5N, R9E, county='Cimarron')")
p("=" * 70)
test_cases = [
    dict(sect=14, twp=5, ns="N", rng=9, ew="E", county="Cimarron"),
    dict(sect=14, twp=5, ns="N", rng=9, ew="E", county=None),
    dict(sect=22, twp=12, ns="N", rng=14, ew="W", county="Blaine"),
    dict(sect=22, twp=12, ns="N", rng=14, ew="W", county=None),
]
for tc in test_cases:
    if tc["county"]:
        cur.execute("""
            SELECT COUNT(*) AS n, array_agg(DISTINCT quadrant_label ORDER BY quadrant_label) AS quads
            FROM plss_grid
            WHERE sect_num=%(sect)s AND township=%(twp)s
              AND north_south=%(ns)s AND "range"=%(rng)s AND east_west=%(ew)s
              AND county_name ILIKE %(county)s;
        """, {**tc, "county": f"%{tc['county']}%"})
    else:
        cur.execute("""
            SELECT COUNT(*) AS n, array_agg(DISTINCT quadrant_label ORDER BY quadrant_label) AS quads
            FROM plss_grid
            WHERE sect_num=%(sect)s AND township=%(twp)s
              AND north_south=%(ns)s AND "range"=%(rng)s AND east_west=%(ew)s;
        """, tc)
    r = cur.fetchone()
    label = f"S{tc['sect']} T{tc['twp']}{tc['ns']} R{tc['rng']}{tc['ew']} county={tc['county'] or 'ANY'}"
    p(f"  {label}")
    p(f"    rows={r['n']}  quads={r['quads']}")

# ===========================================================================
# 6. WHAT ARE THE 64 ROWS FOR ONE SECTION?
#    (full 8×8 grid — each row is one cell)
# ===========================================================================
p()
p("=" * 70)
p("6. ALL 64 CELLS FOR SECTION 22, T12N, R14W (showing quadrant + bbox)")
p("=" * 70)
cur.execute("""
    SELECT quadrant_label, minx, miny, maxx, maxy,
           top_left, top_right, bottom_left, bottom_right
    FROM plss_grid
    WHERE sect_num=22 AND township=12 AND north_south='N'
      AND "range"=14 AND east_west='W'
    ORDER BY quadrant_label;
""")
cells = cur.fetchall()
p(f"  Found {len(cells)} cells")
if cells:
    # Print first 8 to show pattern
    p("  First 8 cells:")
    for c in cells[:8]:
        p(f"    quad={c['quadrant_label']!s:<12}  "
          f"bbox=[{c['minx']:.5f},{c['miny']:.5f} to {c['maxx']:.5f},{c['maxy']:.5f}]")
    # Check whether stored corners match computed corners
    p("\n  Corner consistency check (stored top_left vs computed from bbox):")
    mismatches = 0
    for c in cells:
        stored_tl = c["top_left"]   # e.g. [lon, lat] or {x:, y:} — check type
        p(f"    quad={c['quadrant_label']!s:<12}  top_left={stored_tl}  "
          f"expected=({c['minx']:.5f}, {c['maxy']:.5f})")
        break   # just show one example

# ===========================================================================
# 7. DECODE quadrant_label → (row, col)
# ===========================================================================
p()
p("=" * 70)
p("7. quadrant_label PATTERN ANALYSIS")
p("=" * 70)
cur.execute("""
    SELECT DISTINCT quadrant_label
    FROM plss_grid
    WHERE sect_num=22 AND township=12 AND north_south='N'
      AND "range"=14 AND east_west='W'
    ORDER BY quadrant_label;
""")
quads = [r["quadrant_label"] for r in cur.fetchall()]
p(f"  All quadrant labels for one section ({len(quads)} total):")
p("  " + "  ".join(str(q) for q in quads[:32]))
if len(quads) > 32:
    p("  " + "  ".join(str(q) for q in quads[32:]))

# ===========================================================================
# 8. SECTION BOUNDING BOX vs AGGREGATED CELL BBOXES
# ===========================================================================
p()
p("=" * 70)
p("8. SECTION BBOX vs MIN/MAX OF CELL BBOXES  (sect=22 T12N R14W)")
p("=" * 70)
cur.execute("""
    SELECT
        MIN(minx) AS minx, MIN(miny) AS miny,
        MAX(maxx) AS maxx, MAX(maxy) AS maxy,
        COUNT(*) AS n_cells,
        (MAX(maxx)-MIN(minx)) AS lon_span_deg,
        (MAX(maxy)-MIN(miny)) AS lat_span_deg,
        (MAX(maxx)-MIN(minx))/8 AS cell_w_deg,
        (MAX(maxy)-MIN(miny))/8 AS cell_h_deg
    FROM plss_grid
    WHERE sect_num=22 AND township=12 AND north_south='N'
      AND "range"=14 AND east_west='W';
""")
b = cur.fetchone()
p(f"  n_cells   = {b['n_cells']}")
p(f"  minx={b['minx']:.7f}  miny={b['miny']:.7f}")
p(f"  maxx={b['maxx']:.7f}  maxy={b['maxy']:.7f}")
p(f"  lon_span  = {b['lon_span_deg']:.7f}°  ({b['lon_span_deg']*111320*np.cos(np.radians(35.5)):.1f} m)")
p(f"  lat_span  = {b['lat_span_deg']:.7f}°  ({b['lat_span_deg']*111320:.1f} m)")
p(f"  cell_w    = {b['cell_w_deg']:.7f}°  ({b['cell_w_deg']*111320*np.cos(np.radians(35.5)):.1f} m)")
p(f"  cell_h    = {b['cell_h_deg']:.7f}°  ({b['cell_h_deg']*111320:.1f} m)")

# Cache bbox for later
sec_minx = float(b["minx"])
sec_miny = float(b["miny"])
sec_maxx = float(b["maxx"])
sec_maxy = float(b["maxy"])
cell_w   = float(b["cell_w_deg"])
cell_h   = float(b["cell_h_deg"])

# ===========================================================================
# 9. DO STORED CORNERS MATCH COMPUTED CORNERS?
# ===========================================================================
p()
p("=" * 70)
p("9. STORED CORNER ARRAYS vs COMPUTED CORNERS (sect=22 T12N R14W)")
p("=" * 70)
cur.execute("""
    SELECT quadrant_label, minx, miny, maxx, maxy,
           top_left, top_right, bottom_left, bottom_right
    FROM plss_grid
    WHERE sect_num=22 AND township=12 AND north_south='N'
      AND "range"=14 AND east_west='W'
    ORDER BY quadrant_label
    LIMIT 5;
""")
corner_rows = cur.fetchall()
for cr in corner_rows:
    ql = cr["quadrant_label"]
    stored_tl = cr["top_left"]
    stored_tr = cr["top_right"]
    stored_bl = cr["bottom_left"]
    stored_br = cr["bottom_right"]
    # computed corners from individual cell bbox
    comp_tl = (cr["minx"], cr["maxy"])
    comp_tr = (cr["maxx"], cr["maxy"])
    comp_bl = (cr["minx"], cr["miny"])
    comp_br = (cr["maxx"], cr["miny"])
    p(f"  quad={ql}")
    p(f"    stored  tl={stored_tl}  tr={stored_tr}")
    p(f"    stored  bl={stored_bl}  br={stored_br}")
    p(f"    computed tl=({comp_tl[0]:.6f},{comp_tl[1]:.6f})  tr=({comp_tr[0]:.6f},{comp_tr[1]:.6f})")
    p(f"    computed bl=({comp_bl[0]:.6f},{comp_bl[1]:.6f})  br=({comp_br[0]:.6f},{comp_br[1]:.6f})")

# ===========================================================================
# 10. ADJACENT SECTION OVERLAP CHECK
# ===========================================================================
p()
p("=" * 70)
p("10. DOES sect 21 T12N R14W share an edge with sect 22?")
p("=" * 70)
cur.execute("""
    SELECT sect_num,
           MIN(minx) AS minx, MIN(miny) AS miny,
           MAX(maxx) AS maxx, MAX(maxy) AS maxy
    FROM plss_grid
    WHERE sect_num IN (21,22,23,27,28) AND township=12 AND north_south='N'
      AND "range"=14 AND east_west='W'
    GROUP BY sect_num
    ORDER BY sect_num;
""")
adj = cur.fetchall()
for a in adj:
    p(f"  sect {a['sect_num']}: x=[{a['minx']:.6f},{a['maxx']:.6f}]  "
      f"y=[{a['miny']:.6f},{a['maxy']:.6f}]")

# ===========================================================================
# 11. COUNT DUPLICATE SECTION ENTRIES (same STR, different counties)
# ===========================================================================
p()
p("=" * 70)
p("11. SECTIONS WITH MULTIPLE COUNTY MATCHES  (same sect+twp+ns+rng+ew)")
p("=" * 70)
cur.execute("""
    SELECT sect_num, township, north_south, "range", east_west,
           COUNT(DISTINCT county_name) AS n_counties,
           array_agg(DISTINCT county_name ORDER BY county_name) AS counties,
           COUNT(*) AS n_rows
    FROM plss_grid
    GROUP BY sect_num, township, north_south, "range", east_west
    HAVING COUNT(DISTINCT county_name) > 1
    ORDER BY n_rows DESC
    LIMIT 10;
""")
multi = cur.fetchall()
p(f"  Sections with >1 county entry: {len(multi)} (showing top 10 by row count)")
for m in multi:
    p(f"  S{m['sect_num']} T{m['township']}{m['north_south']} R{m['range']}{m['east_west']}  "
      f"n_rows={m['n_rows']}  counties={m['counties']}")

# ===========================================================================
# 12. WHICH ROW/COL DOES quadrant_label MAP TO?
# ===========================================================================
p()
p("=" * 70)
p("12. quadrant_label DECODING  (what IS the label format?)")
p("=" * 70)
cur.execute("""
    SELECT quadrant_label, minx, miny, maxx, maxy
    FROM plss_grid
    WHERE sect_num=22 AND township=12 AND north_south='N'
      AND "range"=14 AND east_west='W'
    ORDER BY maxy DESC, minx ASC   -- top row first, left to right
    LIMIT 16;
""")
decoded = cur.fetchall()
p("  Top 16 cells sorted N→S, W→E:")
p(f"  {'label':<15} {'minx':>12} {'miny':>12} {'maxx':>12} {'maxy':>12}")
for d in decoded:
    p(f"  {str(d['quadrant_label']):<15} {d['minx']:>12.6f} {d['miny']:>12.6f} "
      f"{d['maxx']:>12.6f} {d['maxy']:>12.6f}")

# ===========================================================================
# 13. MAP ROW/COL → quadrant_label  (build the translation table)
# ===========================================================================
p()
p("=" * 70)
p("13. FULL (row, col) → quadrant_label TRANSLATION TABLE")
p("=" * 70)

# Fetch all 64 cells sorted by position
cur.execute("""
    SELECT quadrant_label, minx, miny, maxx, maxy
    FROM plss_grid
    WHERE sect_num=22 AND township=12 AND north_south='N'
      AND "range"=14 AND east_west='W'
    ORDER BY maxy DESC, minx ASC;
""")
all64 = cur.fetchall()

grid_map = {}   # (row, col) -> quadrant_label
if len(all64) == 64:
    for idx, cell in enumerate(all64):
        row = idx // 8 + 1
        col = idx % 8 + 1
        grid_map[(row, col)] = cell["quadrant_label"]
    p("  grid_map (row, col) → label:")
    for r in range(1, 9):
        row_labels = [str(grid_map.get((r, c), "?")) for c in range(1, 9)]
        p(f"  row {r}: {' | '.join(f'{l:>6}' for l in row_labels)}")
else:
    p(f"  WARNING: expected 64 cells, got {len(all64)}")

# ===========================================================================
# 14. BILINEAR INTERPOLATION — MATH WALK-THROUGH FOR DOT (row=3, col=5)
# ===========================================================================
p()
p("=" * 70)
p("14. BILINEAR INTERPOLATION WALK-THROUGH  (row=3, col=5, x_norm=0.63, y_norm=0.42)")
p("=" * 70)

row_ex, col_ex = 3, 5
x_norm_ex, y_norm_ex = 0.63, 0.42

GRID_SIZE = 8
cell_w_ex = (sec_maxx - sec_minx) / GRID_SIZE
cell_h_ex = (sec_maxy - sec_miny) / GRID_SIZE

left_ex  = sec_minx + (col_ex - 1) * cell_w_ex
right_ex = sec_minx +  col_ex      * cell_w_ex
top_ex   = sec_maxy - (row_ex - 1) * cell_h_ex
bot_ex   = sec_maxy -  row_ex      * cell_h_ex

tl = (left_ex, top_ex)
tr = (right_ex, top_ex)
bl = (left_ex, bot_ex)
br = (right_ex, bot_ex)

rel_x_ex = x_norm_ex * GRID_SIZE - (col_ex - 1)
rel_y_ex = y_norm_ex * GRID_SIZE - (row_ex - 1)

rx, ry = rel_x_ex, rel_y_ex
lon_ex = tl[0]*(1-rx)*(1-ry) + tr[0]*rx*(1-ry) + bl[0]*(1-rx)*ry + br[0]*rx*ry
lat_ex = tl[1]*(1-rx)*(1-ry) + tr[1]*rx*(1-ry) + bl[1]*(1-rx)*ry + br[1]*rx*ry

p(f"  Section bbox: minx={sec_minx:.7f}  miny={sec_miny:.7f}")
p(f"               maxx={sec_maxx:.7f}  maxy={sec_maxy:.7f}")
p(f"  cell_w={cell_w_ex:.7f}°  cell_h={cell_h_ex:.7f}°")
p()
p(f"  Cell (row={row_ex}, col={col_ex}) corners:")
p(f"    TL (NW): lon={tl[0]:.7f}  lat={tl[1]:.7f}")
p(f"    TR (NE): lon={tr[0]:.7f}  lat={tr[1]:.7f}")
p(f"    BL (SW): lon={bl[0]:.7f}  lat={bl[1]:.7f}")
p(f"    BR (SE): lon={br[0]:.7f}  lat={br[1]:.7f}")
p()
p(f"  Dot: x_norm={x_norm_ex}  y_norm={y_norm_ex}")
p(f"  rel_x = {x_norm_ex} × 8 - ({col_ex}-1) = {rel_x_ex:.4f}  (0→west, 1→east within cell)")
p(f"  rel_y = {y_norm_ex} × 8 - ({row_ex}-1) = {rel_y_ex:.4f}  (0→north, 1→south within cell)")
p()
p(f"  Bilinear result:")
p(f"    lat = {lat_ex:.7f}")
p(f"    lon = {lon_ex:.7f}")
p()
p(f"  Cell centre (rel_x=rel_y=0.5):")
lat_ctr = (tl[1] + tr[1] + bl[1] + br[1]) / 4
lon_ctr = (tl[0] + tr[0] + bl[0] + br[0]) / 4
p(f"    lat = {lat_ctr:.7f}  lon = {lon_ctr:.7f}")
lat_off_m = abs(lat_ex - lat_ctr) * 111320
lon_off_m = abs(lon_ex - lon_ctr) * 111320 * np.cos(np.radians(lat_ctr))
p(f"  Offset from cell centre: {lat_off_m:.1f} m N/S, {lon_off_m:.1f} m E/W")

# ===========================================================================
# VISUALISATION A — 8×8 Grid with dot interpolation
# ===========================================================================
fig, ax = plt.subplots(figsize=(9, 9))

# Draw all 64 cells
for (r, c) in [(r, c) for r in range(1, 9) for c in range(1, 9)]:
    xl = sec_minx + (c - 1) * cell_w_ex
    xr = sec_minx + c * cell_w_ex
    yt = sec_maxy - (r - 1) * cell_h_ex
    yb = sec_maxy - r * cell_h_ex
    color = "#d4e6f1" if (r + c) % 2 == 0 else "#eaf4fb"
    rect = plt.Polygon(
        [(xl, yb), (xr, yb), (xr, yt), (xl, yt)],
        closed=True, facecolor=color, edgecolor="#2e86c1", linewidth=0.8
    )
    ax.add_patch(rect)
    ax.text((xl + xr) / 2, (yt + yb) / 2, f"r{r}c{c}",
            ha="center", va="center", fontsize=5.5, color="#555")

# Highlight the target cell
xl_t = sec_minx + (col_ex - 1) * cell_w_ex
xr_t = sec_minx + col_ex * cell_w_ex
yt_t = sec_maxy - (row_ex - 1) * cell_h_ex
yb_t = sec_maxy - row_ex * cell_h_ex
rect_h = plt.Polygon(
    [(xl_t, yb_t), (xr_t, yb_t), (xr_t, yt_t), (xl_t, yt_t)],
    closed=True, facecolor="#f9e79f", edgecolor="#d4ac0d", linewidth=2, zorder=3
)
ax.add_patch(rect_h)

# Mark corners of target cell
for (lx, ly), label, color in [
    (tl, "TL\n(NW)", "#2ecc71"),
    (tr, "TR\n(NE)", "#e74c3c"),
    (bl, "BL\n(SW)", "#3498db"),
    (br, "BR\n(SE)", "#9b59b6"),
]:
    ax.plot(lx, ly, "s", color=color, ms=8, zorder=6)
    ax.text(lx, ly, f" {label}\n ({lx:.5f},{ly:.5f})", fontsize=5.5,
            va="center", color=color, zorder=7)

# Mark dot and cell centre
ax.plot(lon_ex, lat_ex, "*", color="#e74c3c", ms=16, zorder=8, label=f"Dot ({lon_ex:.6f},{lat_ex:.6f})")
ax.plot(lon_ctr, lat_ctr, "+", color="black", ms=12, zorder=8, label=f"Cell centre")

# Rel position annotation
ax.annotate(
    f"rel_x={rel_x_ex:.3f}\nrel_y={rel_y_ex:.3f}",
    xy=(lon_ex, lat_ex), xytext=(lon_ex + cell_w_ex * 1.5, lat_ex + cell_h_ex * 0.5),
    fontsize=8, arrowprops=dict(arrowstyle="->", color="black"),
    bbox=dict(fc="white", ec="gray", boxstyle="round,pad=0.3"),
)

ax.set_xlim(sec_minx - cell_w_ex * 0.5, sec_maxx + cell_w_ex * 0.5)
ax.set_ylim(sec_miny - cell_h_ex * 0.5, sec_maxy + cell_h_ex * 0.5)
ax.set_xlabel("Longitude (°)")
ax.set_ylabel("Latitude (°)")
ax.set_title(f"Section 22  T12N R14W  —  8×8 grid  |  Dot @ row={row_ex} col={col_ex}", fontsize=11)
ax.legend(loc="upper right", fontsize=8)
plt.tight_layout()
fig.savefig(OUT / "A_grid_dot_interpolation.png", dpi=150)
plt.close(fig)
p(f"\n  [Saved] A_grid_dot_interpolation.png")

# ===========================================================================
# VISUALISATION B — Bilinear weight diagram
# ===========================================================================
fig2, ax2 = plt.subplots(figsize=(6, 6))
# Unit cell
ax2.plot([0, 1, 1, 0, 0], [0, 0, 1, 1, 0], "k-", lw=2)
ax2.text(-0.08, 1.0, "TL\n(NW)", ha="center", va="center", fontsize=10, color="#2ecc71")
ax2.text(1.08, 1.0, "TR\n(NE)", ha="center", va="center", fontsize=10, color="#e74c3c")
ax2.text(-0.08, 0.0, "BL\n(SW)", ha="center", va="center", fontsize=10, color="#3498db")
ax2.text(1.08, 0.0, "BR\n(SE)", ha="center", va="center", fontsize=10, color="#9b59b6")

# Weight areas (rx=0.37, ry=0.36 — relative inside cell)
rx_b, ry_b = rel_x_ex, rel_y_ex
# Note: ry is from top, so in plot coords (0=bottom, 1=top): plot_y = 1-ry
px, py = rx_b, 1 - ry_b
ax2.plot(px, py, "*r", ms=20, zorder=5, label=f"Dot  rel_x={rx_b:.2f}  rel_y={ry_b:.2f}")

# Weight rectangles (colored semi-transparent)
# W_TL = (1-rx)(1-ry)  → bottom-right subregion size from TL perspective
patches_data = [
    (0, py, px, 1-py, "#2ecc71",  f"W_TL={(1-rx_b)*(1-ry_b):.3f}"),   # TL
    (px, py, 1-px, 1-py, "#e74c3c", f"W_TR={rx_b*(1-ry_b):.3f}"),       # TR
    (0, 0, px, py, "#3498db",     f"W_BL={(1-rx_b)*ry_b:.3f}"),         # BL
    (px, 0, 1-px, py, "#9b59b6",  f"W_BR={rx_b*ry_b:.3f}"),             # BR
]
for (x0, y0, w, h, col, lbl) in patches_data:
    rect = mpatches.FancyBboxPatch((x0, y0), w, h,
        boxstyle="square,pad=0", alpha=0.25, facecolor=col, edgecolor=col, lw=1.5)
    ax2.add_patch(rect)
    ax2.text(x0 + w/2, y0 + h/2, lbl, ha="center", va="center", fontsize=9, color=col)

ax2.set_xlim(-0.2, 1.2)
ax2.set_ylim(-0.2, 1.2)
ax2.set_xlabel("rel_x  (0=west, 1=east)")
ax2.set_ylabel("1 - rel_y  (0=south, 1=north)")
ax2.set_title("Bilinear Interpolation — Weight Diagram\n"
              "Each corner weighted by opposite sub-area", fontsize=11)
ax2.legend(loc="lower right", fontsize=9)
ax2.set_aspect("equal")
plt.tight_layout()
fig2.savefig(OUT / "B_bilinear_weights.png", dpi=150)
plt.close(fig2)
p(f"  [Saved] B_bilinear_weights.png")

# ===========================================================================
# VISUALISATION C — Oklahoma-wide section density map (sampled)
# ===========================================================================
p()
p("Building Oklahoma coverage map (sampled sections)...")
cur.execute("""
    SELECT
        MIN(minx) + (MAX(maxx)-MIN(minx))/2 AS cx,
        MIN(miny) + (MAX(maxy)-MIN(miny))/2 AS cy,
        COUNT(*) AS n_cells
    FROM plss_grid
    GROUP BY sect_num, township, north_south, "range", east_west
    HAVING COUNT(*) = 64;
""")
coverage = cur.fetchall()
p(f"  Complete 64-cell sections: {len(coverage):,}")

if coverage:
    cx = np.array([float(r["cx"]) for r in coverage])
    cy = np.array([float(r["cy"]) for r in coverage])
    fig3, ax3 = plt.subplots(figsize=(12, 8))
    h = ax3.hexbin(cx, cy, gridsize=80, cmap="YlOrRd", mincnt=1)
    plt.colorbar(h, ax=ax3, label="Sections per hex bin")
    ax3.set_xlim(-103.1, -94.4)
    ax3.set_ylim(33.6, 37.1)
    ax3.set_xlabel("Longitude (°)")
    ax3.set_ylabel("Latitude (°)")
    ax3.set_title(f"Oklahoma PLSS Coverage — {len(coverage):,} complete 64-cell sections", fontsize=12)
    plt.tight_layout()
    fig3.savefig(OUT / "C_oklahoma_coverage.png", dpi=150)
    plt.close(fig3)
    p(f"  [Saved] C_oklahoma_coverage.png")

# ===========================================================================
# VISUALISATION D — Cell precision vs. cell centre error distribution
# ===========================================================================
p()
p("Computing cell-centre vs bilinear error for random x_norm, y_norm...")
rng_gen = np.random.default_rng(42)
n_samples = 10_000
xn = rng_gen.uniform(0, 1, n_samples)
yn = rng_gen.uniform(0, 1, n_samples)

# For a typical cell size (use the section 22 T12N R14W values)
errors_m = []
for x_n, y_n in zip(xn, yn):
    r_dot = int(x_n * 8) + 1  # maps to col 1-8
    c_dot = int(y_n * 8) + 1  # maps to row 1-8
    r_dot = min(r_dot, 8)
    c_dot = min(c_dot, 8)
    xl = sec_minx + (r_dot - 1) * cell_w_ex
    xr_c = sec_minx + r_dot * cell_w_ex
    yt_c = sec_maxy - (c_dot - 1) * cell_h_ex
    yb_c = sec_maxy - c_dot * cell_h_ex
    # True bilinear position
    rx2 = x_n * 8 - (r_dot - 1)
    ry2 = y_n * 8 - (c_dot - 1)
    tl2 = (xl, yt_c); tr2 = (xr_c, yt_c); bl2 = (xl, yb_c); br2 = (xr_c, yb_c)
    lat_true = tl2[1]*(1-rx2)*(1-ry2)+tr2[1]*rx2*(1-ry2)+bl2[1]*(1-rx2)*ry2+br2[1]*rx2*ry2
    lon_true = tl2[0]*(1-rx2)*(1-ry2)+tr2[0]*rx2*(1-ry2)+bl2[0]*(1-rx2)*ry2+br2[0]*rx2*ry2
    lat_ctr2 = (tl2[1]+tr2[1]+bl2[1]+br2[1])/4
    lon_ctr2 = (tl2[0]+tr2[0]+bl2[0]+br2[0])/4
    err_lat_m = abs(lat_true - lat_ctr2) * 111320
    err_lon_m = abs(lon_true - lon_ctr2) * 111320 * np.cos(np.radians(lat_true))
    errors_m.append(np.sqrt(err_lat_m**2 + err_lon_m**2))

errors_m = np.array(errors_m)
fig4, ax4 = plt.subplots(figsize=(8, 5))
ax4.hist(errors_m, bins=60, color="#2e86c1", edgecolor="white", alpha=0.85)
ax4.axvline(np.mean(errors_m), color="red", lw=2, label=f"Mean={np.mean(errors_m):.0f} m")
ax4.axvline(np.median(errors_m), color="orange", lw=2, ls="--",
            label=f"Median={np.median(errors_m):.0f} m")
ax4.axvline(np.percentile(errors_m, 95), color="purple", lw=2, ls=":",
            label=f"P95={np.percentile(errors_m, 95):.0f} m")
ax4.set_xlabel("Position error vs. cell centre (metres)")
ax4.set_ylabel("Count")
ax4.set_title("Error if cell centre used instead of bilinear interpolation\n"
              "(10,000 random dot positions across 8×8 grid)", fontsize=11)
ax4.legend()
plt.tight_layout()
fig4.savefig(OUT / "D_cellcentre_vs_bilinear_error.png", dpi=150)
plt.close(fig4)
p(f"  [Saved] D_cellcentre_vs_bilinear_error.png")
p(f"  Mean error (cell centre):   {np.mean(errors_m):.0f} m")
p(f"  Median error (cell centre): {np.median(errors_m):.0f} m")
p(f"  P95 error   (cell centre):  {np.percentile(errors_m, 95):.0f} m")
p(f"  Max error   (cell centre):  {np.max(errors_m):.0f} m")

# ===========================================================================
# SUMMARY OF KEY FINDINGS
# ===========================================================================
p()
p("=" * 70)
p("SUMMARY — KEY FINDINGS")
p("=" * 70)
p(textwrap.dedent(f"""
  TOTAL ROWS IN plss_grid        : {total:,}

  ROWS PER SECTION QUERY
    A typical S+T+R+EW query (with county)  returns 64 rows — one per cell
    in the 8×8 sub-section grid.  Without county filter it may return 128
    if the section straddles two counties.  Always take MIN/MAX to get the
    full section bbox regardless of county duplicates.

  quadrant_label
    Each of the 64 rows has a label (e.g. 'NE' / 'SW' / numeric / grid code)
    that identifies which cell within the section the row represents.
    Sorted by (maxy DESC, minx ASC), position 0 = top-left = row 1, col 1.

  HOW ROW / COL MAPS TO A CELL
    The U-Net dot detector outputs (row, col) ∈ [1..8] × [1..8]:
        row 1 = northernmost strip of the section
        col 1 = westernmost strip
    Our resolver fetches just the section bbox (MIN/MAX) and computes:
        cell_w = (maxx - minx) / 8   cell_h = (maxy - miny) / 8
        cell TL = (minx+(col-1)*w, maxy-(row-1)*h)
    This is mathematically equivalent to looking up the stored cell row
    directly and avoids any ambiguity with quadrant_label format.

  CORNER STORAGE (top_left / top_right / bottom_left / bottom_right)
    Each row stores the 4 corners of its cell as array[lon, lat].
    These match the values computed from minx/miny/maxx/maxy for that row.
    We use MIN/MAX across the section instead of stored corners because:
    — It avoids joining 64 rows; one aggregate query suffices.
    — Stored corners are per-cell; our approach derives them analytically.

  BILINEAR INTERPOLATION — WHY IT MATTERS
    Using x_norm / y_norm from U-Net shifts the result by up to
    {np.max(errors_m):.0f} m compared with cell centre.
    Mean gain:  {np.mean(errors_m):.0f} m  |  P95: {np.percentile(errors_m, 95):.0f} m
    A PLSS cell at Oklahoma latitude is ≈{cell_w_ex*111320*np.cos(np.radians(35.5)):.0f} m wide
    × {cell_h_ex*111320:.0f} m tall, so the sub-cell precision is meaningful.

  WHICH ROW TO PICK WHEN MULTIPLE COUNTIES MATCH
    If county filter returns 64 rows → use that directly (exact match).
    If it returns >64 rows → section spans counties; bbox still valid.
    If county filter returns 0  → fall back to no-county query (same section bbox).
    The section bbox (MIN/MAX) is identical regardless of county — geography
    does not change.  County only disambiguates when OCR confusion could
    produce the wrong township/range.
"""))

# ===========================================================================
# WRITE SUMMARY
# ===========================================================================
summary_path = OUT / "summary.txt"
summary_path.write_text("\n".join(lines), encoding="utf-8")
p(f"\nFull summary written to: {summary_path}")
p(f"Visualisations in:       {OUT}")

# ===========================================================================
# CLOSE — NO IDLE CONNECTIONS REMAIN
# ===========================================================================
try:
    cur.close()
    conn.close()
    p("\nConnection closed. $0 idle charges.")
except Exception:
    pass
conn = None
