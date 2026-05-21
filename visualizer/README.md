# OSU Well Records — Interactive Map Viewer

Interactive map of all Oklahoma oil and gas well permit locations extracted by the OSU pipeline.  
Pins are colored by decade. Click any pin for full well details and a link to the source PDF.

## Quick Start

```powershell
# Refresh map data from latest pipeline output + open in browser
cd D:\project_modular\visualizer
.\refresh_map.ps1
```

## Files

| File | Purpose |
|---|---|
| `merge_well_locations.py` | Downloads `dot_coordinates.csv` from all completed S3 slices, filters valid Oklahoma coords, outputs `well_locations.json` + `well_locations.csv` |
| `well_map.html` | Map viewer template (fetches GeoJSON from S3 at runtime) |
| `build_standalone.py` | Embeds `well_locations.json` directly into HTML for offline/single-file use |
| `deploy_viewer.py` | Uploads HTML + data to S3 with public-read policy |
| `refresh_map.ps1` | One-click: merge + build + upload + open browser |
| `well_map_standalone.html` | **Generated** — self-contained HTML with embedded data |
| `well_locations.json` | **Generated** — GeoJSON FeatureCollection of all well locations |
| `well_locations.csv` | **Generated** — flat CSV for analysis |
| `viewer_url.txt` | **Generated** — 7-day presigned S3 URL for sharing |

## Map Features

- **Marker clustering** — groups nearby wells at low zoom; expands on zoom in
- **Decade color coding** — each decade (1910s–2020s) has a distinct color
- **Rich popups** — well name, API number, collection, year, county, PLSS location (Sec/Twp/Rng), coordinates, confidence, PDF link
- **Filters** — by county, collection number, decade, well name/ID
- **Legend** — click any decade to hide/show those markers
- **Map layers** — Dark (default), Satellite (Esri), Roadmap (OSM)

## Data Source

Coordinates are resolved via `quadrant_direct` mapping: the pipeline detects the well dot  
on the PLSS grid image, identifies the Section/Township/Range cell, then computes lat/lon  
from the dot's normalized position within that cell.

| Resolution method | Meaning |
|---|---|
| `quadrant_direct` | Dot position mapped directly to lat/lon via cell bounds |
| `rds_miss` | PLSS RDS lookup returned no matching cell |
| `bounds_invalid` | OCR could not read Township or Range |

## S3 Locations

```
s3://osu-well-records-225989338968/viewer/well_map.html
s3://osu-well-records-225989338968/viewer/well_map_standalone.html
s3://osu-well-records-225989338968/viewer/well_locations.json
s3://osu-well-records-225989338968/viewer/well_locations.csv
```

## Updating After Pipeline Runs

Run `.\refresh_map.ps1` any time to pull in new results:
- First run: ~160 completed slices, ~8,000 wells
- Final run (after all 2,832 slices): ~200,000+ wells expected

## Integration

- **D: drive**: `D:\project_modular\visualizer\`
- **GitHub**: `visualizer/` directory in the repo
- **AWS S3**: `viewer/` prefix in the pipeline bucket
- **Report**: Viewer URL included in `OSU_Pipeline_Report.docx` Appendix
