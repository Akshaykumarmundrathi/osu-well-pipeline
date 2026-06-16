# Oklahoma Well Records — Reference & Audit (1911–2024)

A complete reference to the **source record files** themselves: how many, how
they are organised, how the forms changed across a century, the structural
patterns by era, and the full processing audit (what succeeded, what was
skipped, and every failure type with counts). Generated 2026-06-13 from
`dataset_index.csv`, `master_ledger.csv`, and `form_structures.csv`.

---

## 1. The corpus at a glance

- **571,446 unique well-record PDFs** (deduplicated; ZIP archives and extracted
  folders hold the identical set).
- **1911 → 2024** — 113 years of Oklahoma Corporation Commission well filings.
- Organised into **13 collections** (batches), each spanning a block of years,
  each foldered by `year / month`.

### Collection timeline

| Coll | Years | Records | Year-months | Era character |
|--|--|--|--|--|
| C1 | 1911–1925 | 54,979 | 180 | earliest; faded, handwritten |
| C2 | 1926–1940 | 46,492 | 180 | hollow-circle well marks |
| C3 | 1941–1950 | 41,545 | 120 | wartime/postwar typed forms |
| C4 | 1951–1955 | 48,557 | 60 | settled grid form |
| C5 | 1956–1960 | 42,338 | 60 | settled grid form |
| C6 | 1961–1970 | 53,851 | 120 | clean typed grid forms |
| C7 | 1971–1979 | 52,855 | 108 | clean typed grid forms |
| C8 | 1980–1982 | 49,457 | 36 | OTC completion forms |
| C9 | 1983–1987 | 49,578 | 60 | OTC completion forms |
| C10 | 1988–2000 | 53,492 | 156 | transitional; larger grids |
| C11 | 2001–2012 | 50,991 | 144 | half-digital |
| C12 | 2013–2018 | 19,728 | 72 | **digital-native** completion reports |
| C13 | 2019–2024 | 7,583 | 71 | **digital-native** completion reports |
| **Total** | **1911–2024** | **571,446** | **1,367** | |

---

## 2. How the record format evolved over the century

The thing buried in every record is the **well's legal location** (Section /
Township / Range, and on grid forms a hand-drawn dot marking the spot inside a
1-mile PLSS grid). *Where* and *how* that information sits on the page changed
across eras — this is the core challenge the pipeline solves.

| Era | Collections | Format | Location encoded as |
|--|--|--|--|
| **Early grid** (1911–1925) | C1 | Large form, grid bottom-left, S/T/R in a top header line | hand-drawn dot on grid + header text |
| **Hollow-circle** (1926–1940) | C2 | Grid top-left, S/T/R to the right; well marked with a hollow "o", not a filled dot | circle on grid |
| **Classic typed grid** (1941–1979) | C3–C7 | Grid top-left, S/T/R upper-right or right-of-grid; smaller grids | filled ink dot on grid |
| **OTC completion** (1980–2000) | C8–C10 | Larger/varied grids; more tabular forms | dot on grid + typed fields |
| **Digital-native** (2001–2024) | C11–C13 | Typed completion reports, often **no grid at all**; location written out as text and an API number | typed "Location: county S T R quadrant", well-name STR, API county code |

**The big shift:** pre-2001 records are *scanned paper grid forms* (read with
computer vision + a dot detector). From ~2013 (C12–C13) they are *digital
completion reports* where the data is typed text — a fundamentally different
format that needs a text path, not the grid/dot pipeline.

---

## 3. Record structure types (where the grid and description sit)

Measured from the structural-fingerprint sample (`form_structures.csv`):

**Grid position on the page:** top-left (most common, C2–C9) · bottom-left
(C1) · top-centre · top-right · *(none — digital-native C12–C13)*.

**Legal description (S/T/R) position:** right-of-grid · upper-right · top-header
line · *(typed "Location:" block on modern forms)*.

### Per-collection structural fingerprint (where measured)

| Coll | dominant form type | grid zone | S/T/R zone | typical grid width |
|--|--|--|--|--|
| C1 | T1_LARGE | bottom-left | top-header | ~142 px |
| C2 | T2_MED | top-left | right-of-grid | ~210 px |
| C3 | T4_NOANCHOR | top-left | upper-right | ~189 px |
| C4 | T2_MED | top-left | right-of-grid | ~127 px |
| C5 | T2_MED | top-left | right-of-grid | ~111 px |
| C10 | (varied) | (varied) | any | ~392 px (larger) |

(C6–C13 structural capture pending; C12–C13 are gridless digital reports.)

---

## 4. Processing audit — what's been done so far

State of all **571,446** records:

| State | Count | Meaning |
|--|--|--|
| queued | 503,860 | registered, not yet processed (the cloud backlog) |
| not_processed | 55,798 | not yet registered/run |
| partial | 4,824 | some stages done (most mapped wells are here) |
| failed@location | 3,108 | first failed at the location stage |
| failed@dot | 2,065 | first failed at the dot stage |
| failed@county | 1,005 | first failed at the county stage |
| failed@grid | 605 | first failed at the grid stage |
| failed@latlong | 179 | first failed at the lat/long stage |
| success (all 5 stages) | 2 | (latlong is legitimately blank on most) |

> ~11,800 records have been **attempted** so far (the rest await the cloud run).
> Mapped wells on the live site: **7,163**.

### Per-stage outcomes (of ~11,788 attempted)

| Stage | done | failed | skipped/pending |
|--|--|--|--|
| grid | 10,954 (93%) | 661 | 173 |
| county | 10,006 (85%) | 1,773 | 9 |
| dot | 7,548 (64%) | 3,406 | 834 |
| location | 7,024 (60%) | 3,556 | 1,208 |

### Failure types, with counts

| Stage | failure type | count | what it means |
|--|--|--|--|
| **location** | not_found | 1,650 | S/T/R not located on page |
| | not_detected | 906 | text region found, value unreadable |
| | no_match | 267 | text present but didn't match patterns |
| | exception | 237 | processing error on that record |
| **dot** | not_detected | 2,041 | U-Net saw no dot (incl. hollow circles, gridless forms) |
| | exception | 24 | render/processing error |
| **county** | no_match | 601 | county text didn't match the 77-county list |
| | not_detected | 379 | county region unreadable |
| **grid** | not_found | 321 | no grid located (often gridless modern forms) |
| | not_detected | 133 | grid candidate rejected |
| | no_match | 118 | no grid-shaped region |

---

## 5. Per-collection success map (key insight)

| Coll | era | grid | location | county | dot | headline |
|--|--|--|--|--|--|--|
| C1 | 1911-15 | 92% | 41% | 89% | 59% | location weak (header STR) |
| C2 | 1926-40 | 88% | 48% | 84% | **38%** | hollow circles, not dots |
| C3 | 1941-50 | 98% | 44% | 86% | 63% | location weak |
| C4 | 1950s | 99% | 73% | 84% | 78% | good |
| C5 | 1958+ | 98% | 74% | 81% | 86% | good |
| C6 | 1960s | 99% | 83% | 80% | 93% | strong |
| C7 | 1970s | 99% | 77% | 90% | 91% | strong |
| C8 | 1980s | 99% | 62% | 91% | 85% | good |
| C9 | 1980s | 93% | 66% | 93% | 77% | good |
| C10 | 1988-2000 | 96% | 91% | 86% | **48%** | dot mark style |
| C11 | 2001-12 | 83% | 96% | 75% | **48%** | half-digital |
| C12 | 2013-18 | **48%** | 70% | **36%** | **28%** | digital-native, no grid |
| C13 | 2019-24 | **7%** | **12%** | **34%** | **3%** | digital-native, no grid |

---

## 6. Patterns & insights (the "why")

1. **Failures cluster by era/format, not randomly.** A format quirk breaks the
   same way across a whole decade — fixing one fixes thousands.
2. **C2 (1926-40) dot failures = hollow circles.** The forms mark the well with
   a hollow "o", not a filled dot; the detector was trained on filled dots.
3. **C10 (1988+) dot failures = a different mark/scale**, grids present but the
   detector misses — same class as C2, modern.
4. **Location is the weakest stage on the oldest forms (C1-C3, ~41-48%)** — the
   S/T/R sits in header text the early patterns don't all catch.
5. **C11-C13 are a different problem entirely** — digital-native reports with no
   grid/dot. Grid/dot "failures" there are expected; the location is in typed
   text (API county code, well-name S/T/R like `23-20N-1W`, "Location:" block).
   These need a text path, not the grid pipeline.
6. **County is the most robust field** (84-93% across most eras) thanks to
   structural anchors + Gemini fallback; weakest only on the gridless C12-C13.
7. **The API number deterministically encodes the county** for modern records
   (`35-003`→Alfalfa, `35-011`→Blaine) — a free lever specifically for C11-C13.

---

*Counts are exact from the deduplicated index; per-stage/structure figures are
from records attempted so far (~11.8k) — they will broaden as the cloud run
processes the remaining ~560k. The methodology and code are in the repo.*
