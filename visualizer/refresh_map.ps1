# refresh_map.ps1
# ----------------
# One-click script to refresh the well location map with the latest pipeline output.
# Run from D:\project_modular\visualizer\
#
# What it does:
#   1. Merges all completed slice dot_coordinates.csv from S3 into well_locations.json
#   2. Builds a self-contained HTML with embedded data
#   3. Uploads HTML to S3 and prints a 7-day presigned URL
#   4. Opens the HTML in the default browser (local preview)

$env:PYTHONIOENCODING = "utf-8"
$dir = $PSScriptRoot

Write-Host "`n=== OSU Well Map Refresh ===" -ForegroundColor Cyan

# Step 1: Merge
Write-Host "`n[1/3] Merging well locations from S3..." -ForegroundColor Yellow
python "$dir\merge_well_locations.py"
if ($LASTEXITCODE -ne 0) { Write-Host "ERROR: merge failed" -ForegroundColor Red; exit 1 }

# Step 2: Build standalone HTML
Write-Host "`n[2/3] Building standalone HTML..." -ForegroundColor Yellow
python "$dir\build_standalone.py" --upload
if ($LASTEXITCODE -ne 0) { Write-Host "ERROR: build failed" -ForegroundColor Red; exit 1 }

# Step 3: Open in browser
Write-Host "`n[3/3] Opening local preview..." -ForegroundColor Yellow
$htmlPath = "$dir\well_map_standalone.html"
if (Test-Path $htmlPath) {
    Start-Process $htmlPath
    Write-Host "Opened: $htmlPath" -ForegroundColor Green
}

# Print presigned URL if available
$urlFile = "$dir\viewer_url.txt"
if (Test-Path $urlFile) {
    Write-Host "`n7-day share URL:" -ForegroundColor Cyan
    Get-Content $urlFile
}

Write-Host "`nDone!" -ForegroundColor Green
