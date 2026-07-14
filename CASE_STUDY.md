# Case Study — Digitizing a Century of Oklahoma Well Records

**From 571,446 scanned PDFs (1911–2024) to a live, self-correcting well map — a research prototype inherited in August 2025 and carried to production scale by one graduate student and an AI coding agent.**

*Oklahoma State University, Boone Pickens School of Geology · DOE Award DE-FE0032362 (Anadarko Basin Carbon Management Hub) · Data: Devon Energy · Aug 2025 – Jul 2026 (production repo: 247 commits, May–Jul 2026)*

- Live map: https://akshaykumarmundrathi.github.io/osu-well-pipeline/
- Repository: https://github.com/Akshaykumarmundrathi/osu-well-pipeline

---

## 1. The Problem

Oklahoma's oil- and gas-well completions are documented on "1002A" forms. Roughly **571,446** of them survive only as scanned PDFs spanning **113 years** of changing layouts — typed, handwritten, stamped, skewed, faded. Buried in each is the well's location: a county, a Section–Township–Range (STR) legal description, and on most forms a hand-drawn dot on a "spot well correctly" grid.

Identifying orphaned, abandoned, and idle wells for carbon-management planning requires those locations as coordinates. Manual conversion is slow, error-prone, and unscalable: at even two minutes per record, the archive is ~9 person-years of transcription. The project goal: **an automated, validated pipeline that turns the entire archive into GIS-ready latitude/longitude — with human verification built in.**

## 2. Timeline — From Inherited Prototype to Production

The project long predates the production repository. The git history (first commit May 15, 2026) records only the final build phase; the real arc spans two academic years and two researchers.

| Phase | Dates | What happened |
|---|---|---|
| **Origins (prior researcher)** | 2024 – mid-2025 | An earlier student (Edgar) extracted county, Section/Township/Range, and location descriptions from 1002A PDFs. Foundational assets built in this era: the **PLSS corner-coordinate database** — 4,565,504 rows × 46 columns, computed with GeoDataFrames/MultiPolygons by integrating three geospatial sources (county boundaries, township/range grids, section subdivisions) down to quarter-quarter squares — plus a **Colab notebook** (`OSU_GRID_PROCESS-Code.ipynb`) using Google Vision to detect the "spot well" grid, and a single-PDF extraction script with a folium map. Working prototype; single-document scale |
| **Takeover & gap analysis** | Aug – Nov 2025 | Current author takes over: months of grinding through the inherited notebook and reports to understand the approach, refine ideas and structures, and design new infrastructure. The core diagnosis: the prototype worked on *one PDF at a time in Colab* — no batch processing, no crash recovery, no state tracking, no cloud path. Entering cloud engineering (AWS accounts, S3, RDS) began here |
| **The database wall** | ~Jun – Dec 2025 (~6 months) | The parquet-built PLSS database (4.56M rows) **could not be loaded** in any free environment — Colab sessions died, local memory was insufficient, every workaround failed for half a year. Finally solved by moving the data to **AWS S3 and standing it up as a PostgreSQL/PostGIS database on RDS** — the single unlock that made coordinate resolution possible at scale, and the project's introduction to real cloud infrastructure |
| **The archive arrives** | Dec 2025 | The full corpus lands: ~571,000 scanned PDFs (13 ZIP archives, 1911–2024). Immediately exposed the true gap: a century of *layout variation* the single-form prototype had never seen. Months of studying PDF structures across eras — where the grid, STR, and county actually sit per decade — became the design foundation for the era-aware pipeline |
| **Production build begins** | May 15–23, 2026 | The modular repo: ZIP-native reading, per-record crash recovery, Vision OCR, Gemini county normalization, grid detection (OpenCV, 6 methods), first 1911-era QA set (95/100 grids detected, 94/94 hand-verified correct). First large benchmark — 4,607 records: grid 100%, county 99%, full STR 69%, dot 89%, 3,695 coordinates resolved. Repo consolidation (1-commit seed vs 56-commit working copy) recovered 4 QA assets; tracker schema settled at 44 columns |
| **Coordinate resolution** | May 23–31, 2026 | The RDS PLSS database wired to a multi-pass resolver (§4.5); bilinear dot interpolation. Portability hardening: hardcoded paths → env vars, cv2 guards, S3 error handling, an 863-line `PIPELINE_BLUEPRINT.md` documenting every stage, schema, and threshold |
| **U-Net dot detector** | May–Jun 2026 | 643 hand-labeled dots → U-Net segmentation (192×192) replacing brittle classical CV; regression-gated retraining protocol |
| **Hardening sprints** | Jun 8–12, 2026 | The laptop era: every silent crash root-caused to 7.4 GB RAM; sharded status saves, self-healing watchdogs, run-scoped resume (12.6 s → 2.1 s startup); systematic issue registry (P1–P11) |
| **Human-in-the-loop** | Jun 10–13, 2026 | 2,781-form manual review → per-collection search envelopes; annotation campaigns with drag-box labeling; map Verify/Fix panel; **fully autonomous corrections loop** (GitHub issue → Action → RDS re-resolution → republish, no human intervention) |
| **Data integrity** | Jun 13–16, 2026 | Full reconciliation: disk ⇄ ZIPs ⇄ index ⇄ status ⇄ coordinates ⇄ S3 ⇄ site, deduplicated to one row per record; 493 missing C13 records found and backfilled to S3; non-destructive merge guards (monotonic map, safe_merge) after two near-miss data-loss incidents |
| **The breakthrough** | Jun 16, 2026 | **Modern-text path**: digital-native records (2001–2024) carry typed location data — API-number county decode + text STR + printed decimal lat/lon. Mapped C13 (88%), C12 (98%), C11-digital (27%) in *minutes at $0*. Map: 7,163 → 47,253 wells in one day |
| **Precision & scale** | Jun 16–20, 2026 | 12,662 wells upgraded to exact printed coordinates; C11 grid campaign completed crash-proof via chunk-chain; RDS-derived county→direction ground truth wired into the resolver; AWS cloud-run designed (1,000-vCPU quota approved); AAPG Bulletin manuscript drafted |
| **Current** | Jul 2026 | **51,559 wells live** across all 77 counties; ~470K records awaiting the cloud run |

**The author's core contribution is the gap between the two eras:** an inherited one-PDF-at-a-time Colab prototype became a resumable, era-aware, cloud-deployed production system — new infrastructure (S3/RDS/Batch), the five-stage modular pipeline, the state-tracking/crash-recovery machinery, the per-decade structural insights from studying half a million real forms, and the live self-correcting map.

## 3. Architecture

```
scanned PDF (ZIP / S3)
  └─ PDFDocumentManager (cached page renders, 2× resolution)
       ├─ latlong/   printed coordinates (decimal + DMS) — modern forms
       ├─ grid/      anchor phrase → measured-envelope crop → full-page CV ensemble
       │               └─ era-aware form classifier
       ├─ location/  STR extraction (per-era recipe regions → zone hints → full page)
       ├─ county/    structural anchors → fuzzy (77-county list) → Gemini fallback
       └─ dot/       U-Net segmentation → (row, col) → PLSS quarter-quarter label
  └─ processing_status.csv   (44-column resumable tracker, sharded writes)
  └─ coord/plss_resolver     (RDS, 5-pass, county-pinned direction disambiguation)
  └─ build_map_data.py       → GitHub Pages interactive map
```

**Design law (held throughout): hints and priors *bound and order* the search — they never terminate it.** Every stage has a full-page fallback; suspect detections are flagged, never silently trusted. This is why a century of layout drift degraded results gracefully instead of breaking them.

## 4. Key Findings

**4.1 Extraction strategy must be format-aware.** The archive splits into two regimes. Pre-2001 scanned grid forms need computer vision + paid OCR. Post-2010 digital-native completion reports carry typed text — county is decodable from the API number (`35-011` → Blaine, deterministic), STR from the location block or well name (`23-20N-1W`), and ~31% print exact decimal coordinates. Routing by era turned the "worst" collections (C13 grid success: 7%) into the best (88–98% mapped, free).

**4.2 Free OCR alternatives fail — and the reason matters.** A rigorous pilot (303 Vision-verified records re-run through Tesseract) showed 0% location agreement on old scans; reusing embedded PDF text layers scored 0% county agreement despite "95% text coverage." Root cause: flat text loses page *geometry* — you can't tell the well's county from the operator's address without bounding boxes. **The extraction problem is spatial, not typographic.** Paid document-OCR earns its cost.

**4.3 The section number is the accuracy ceiling on legacy forms.** Township/range parse at 80–85% (their N/S / E/W letters anchor them), but the section is a bare number whose "SEC" label OCR frequently drops — only 44% of location-successful legacy records had a complete STR. County clustering can't infer it (0 of 221 monthly batches were single-county); the grid gives position *within* a section, not the section itself. It must be read, which sets the improvement agenda: better OCR of the location block.

**4.4 Ground-truth beats heuristics.** Read-only mining of the PLSS database showed direction is *deterministic* from county alone for 59/77 (E/W) and 60/77 (N/S) counties — now wired into the resolver, so records that lost their direction suffix resolve exactly. Earlier, the same class of insight (county-pinned E/W) caught a systematic mirror bug that had flipped ~2,000 eastern wells into western Oklahoma.

**4.5 The coordinate resolver is a decision tree, not a lookup.** Resolving OCR'd STR text against 4.56M PLSS cells with minimal database cost drove a layered design: (1) a one-time GROUP BY warm-up builds per-county direction statistics so ambiguous directions are tried in probability order; (2) a 4-priority direction cascade (prior-run constraints → RDS statistics → geographic hard priors like "Panhandle is Range-East only" → global default) resolves records with *no* direction from OCR at all; (3) OCR directions are soft-overridden when county statistics contradict them (fixing systematic W↔E misreads); (4) seven ordered strategies run cheapest-first (exact+county → exact → county-constrained → N/S fallback → E/W fallback → county-name variants → section ±1 for off-by-one OCR errors), stopping at the first unambiguous hit; (5) an exhaustive N/S×E/W sweep recovers strategy misses; (6) missing-field inference reconstructs a dropped township/range from the database, accepted only when one candidate dominates by ≥4×; (7) batch prefetch collapses ~3,500 potential queries per 500-record slice into 2; (8) U-Net vs OCR quadrant disagreements are flagged, never silently resolved; (9) section-centroid is the explicit last resort, labeled as reduced precision. County name is the master disambiguation signal throughout — it shrinks the search space from ~64,000 sections statewide to a few hundred.

**4.6 Constraints shaped the engineering.** The 7.4 GB laptop OOM-killed every long run, including resident orchestrators. The surviving pattern: a near-zero-RAM `cmd` loop dispatching 50-record chunks as fresh processes (all memory freed on exit), idempotent via the status tracker. It processed 6,341 + 35,845-record campaigns crash-free at ~1,000 records/hour, surviving Wi-Fi losses, shutdowns, and resumes.

## 5. Human-in-the-Loop Machinery

- **2,781 hand-reviewed forms** produced measured per-collection search envelopes — the single biggest accuracy lever for the grid/STR stages (C8 grid: 48% → ~100%).
- **Predict-then-correct labeling**: the model proposes, a human accepts or corrects; 643 dot labels feed regression-gated retraining that can't silently degrade existing accuracy.
- **Public corrections loop**: any map viewer can compare a well to its original PDF and submit a fix (including direct lat/lon override); a GitHub Action validates authorization, applies the change, re-resolves against RDS, and republishes — autonomously.
- **Failure transparency**: per-stage failure CSVs (stem, era, error type, image path) regenerate on every publish for human inspection.

## 6. Infrastructure & Cost

| Layer | Choice | Note |
|---|---|---|
| OCR | Google Cloud Vision | The dominant cost (~$1.50/1,000 images); 118K-image disk cache makes re-runs free |
| County normalization | Gemini (free tier) | 7-key rotation, bounded 429 backoff, per-key cooldown |
| Coordinates | PostgreSQL/PostGIS on AWS RDS | 4.56M PLSS cells |
| Storage | S3 (571,446 PDFs, prefix-scoped public read, no public listing) | Site links each pin to its original scan |
| Compute (cloud) | AWS Batch on Fargate — deployed, 1,000-vCPU quota approved | 250 concurrent tasks ≈ full backlog in ~3 hours |
| Publishing | GitHub Pages, monotonic rebuilds | A rebuild can add/update wells, never drop them |

**Spend to date: roughly $70–160** (development-phase Vision + AWS storage/RDS). **To finish: ~$1,350–1,550**, dominated by Vision on the remaining ~470K scanned forms — a budget decision, not an engineering one.

## 7. Incidents & Lessons

1. **Six months stuck on a database that wouldn't load.** The inherited 4.56M-row parquet PLSS database exceeded every free environment — Colab sessions died, local RAM was insufficient, and half a year of workarounds failed. The fix wasn't a cleverer loading trick; it was changing the architecture: S3 for storage, PostgreSQL/PostGIS on RDS for querying. *Lesson: when data outgrows an environment, stop optimizing the load and move the computation to the data — and recognize that wall as the moment a prototype must become infrastructure.*
2. **Two near-miss data losses** (a consolidation script truncated the 514K-row tracker to 3,334; an enrichment step overwrote the coordinates file). Both recovered from backups; both answered with structural guards — additive-only `safe_merge`, monotonic map builds, union-with-backups. *Lesson: at half-million-record scale, merge tooling must be non-destructive by construction.*
3. **A cloud run would have shipped garbage**: the container defaulted to Tesseract (`USE_VISION_API=0`) — the engine proven inaccurate. Caught in a pre-launch scrutiny pass. *Lesson: audit runtime defaults against experimental findings before scaling.*
4. **The site displayed 148 counties for a 77-county state** — unnormalized county strings ("creek" vs "Creek County") split entities. *Lesson: normalize at the publishing boundary, not just extraction.*
5. **Memory-kill forensics**: silent deaths on Windows were traced through event logs to RAM exhaustion, not code bugs — the fix was architectural (fresh-process chunking), not a patch.
6. **Honest negative results saved money**: the Tesseract and embedded-text pilots each cost hours and returned "no" — and prevented a ~$1,500 mistake premised on wrong assumptions.

## 8. Current State & Remaining Work

**Live:** 51,559 wells, all 77 counties, 21,855 precise / 29,704 approximate, each pin linked to its source scan, self-correcting via the map. Repository: 247 commits, full audit trail from first ZIP scan to cloud design.

**Remaining:**
- ~470K legacy scanned forms → the designed AWS Batch run (image build + Vision budget are the only gates)
- 3 queued annotation campaigns (c6/c7/c12) → free per-era envelope tuning
- U-Net round-2 (1926–40 hollow-circle well marks)
- Map payload optimization (52 MB JSON → tiling/clustering) as the corpus completes
- *AAPG Bulletin* manuscript (drafted; poster presented at AAPG Orphaned/Abandoned/Idle/Marginal Wells, Tulsa, March 25–27, 2025)

## 9. Session Logs

Working sessions are journaled under [`logs/history/`](logs/history/) — points of
discussion, actions, thoughts, ideas, and work, as a running companion to the
commit trail:

- [`2026-07-14 — Cloud-native hardening & cost-control review`](logs/history/2026-07-14_cloud-hardening-session.md)
  — full-code scrutiny pass (robustness confirmed; RDS county→direction backfill
  wired into the resolver, `de960f7`); caught a pre-launch cloud bug where the
  container defaulted to Tesseract instead of Vision (`bb311c0`); established the
  cost reality that the legacy backlog is a Vision-budget decision, not an
  engineering gap, and scoped an autonomous Gemini free-tier cool-down for the
  parts that *are* free-limited.

## 10. Takeaways

1. **Route by format era, then optimize per route** — the single decision worth the most wells per dollar.
2. **Spatial context is the moat**: document understanding at scale is about *where* text sits, not just what it says.
3. **Hints bound, never gate** — a fallback-everywhere design survives a century of layout drift.
4. **Idempotence + fresh-process chunking** turns unreliable hardware into a reliable batch system.
5. **Put humans where they're irreplaceable** (ground-truth envelopes, dot labels, public corrections) and automate the rest — 2,781 reviewed forms leveraged into half a million records.
6. **Reconcile everything to one ledger** — disk, archive, index, tracker, coordinates, cloud, site — or drift will quietly eat correctness.

---

*Written from the project's full arc (Aug 2025 – Jul 2026): the inherited prototype artifacts (Scope of Work, Grid Detection Report, the original Colab notebook), the production commit history (May 15 – Jul 12, 2026), issue registry, pilot reports, insight documents (`RECORDS_REFERENCE.md`, `MAP_AND_DATA_INSIGHTS.md`, `DECADE_INSIGHTS.md`, `RDS_INSIGHTS.md`, `PIPELINE_SCRUTINY.md`, `SECURITY_AUDIT.md`, `AWS_RUN_DESIGN.md`), and session logs.*
