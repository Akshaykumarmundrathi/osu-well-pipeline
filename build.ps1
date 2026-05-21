# build.ps1 — Build and optionally push the Docker image.
#
# Usage (from D:\project_modular):
#   .\build.ps1              # build only
#   .\build.ps1 -Push        # build + push to ECR
#   .\build.ps1 -Push -Tag v2
#
# Prerequisites already satisfied:
#   unet_dot_detector.py  — in D:\project_modular\ (copied from D:\well_dot_detector\)
#   unet_best.pth         — in D:\project_modular\
#   Docker Desktop        — running
#   aws configure         — done

param(
    [switch]$Push,
    [string]$Tag = "latest",
    [string]$ECR = "225989338968.dkr.ecr.us-east-1.amazonaws.com/osu-pipeline"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

Write-Host "=== OSU Pipeline Docker Build ===" -ForegroundColor Cyan

# ---- Pre-flight checks -------------------------------------------------------
$required = @("unet_dot_detector.py", "unet_best.pth", "Dockerfile", "project", "aws/run_batch_job.py")
$missing  = @()
foreach ($f in $required) {
    if (-not (Test-Path "$PSScriptRoot\$f")) { $missing += $f }
}
if ($missing.Count -gt 0) {
    Write-Host "ERROR: missing required files:" -ForegroundColor Red
    $missing | ForEach-Object { Write-Host "  $_" -ForegroundColor Red }
    exit 1
}
Write-Host "Pre-flight OK — all required files present" -ForegroundColor Green

# ---- Docker build ------------------------------------------------------------
Write-Host "`nBuilding osu-pipeline:$Tag ..." -ForegroundColor Cyan
docker build -t "osu-pipeline:$Tag" .
if ($LASTEXITCODE -ne 0) {
    Write-Host "docker build FAILED (exit $LASTEXITCODE)" -ForegroundColor Red
    exit 1
}
Write-Host "Build complete: osu-pipeline:$Tag" -ForegroundColor Green

# ---- ECR push (optional) -----------------------------------------------------
if ($Push) {
    $registry = ($ECR -split "/")[0]
    Write-Host "`nLogging in to ECR ($registry) ..." -ForegroundColor Cyan
    aws ecr get-login-password --region us-east-1 |
        docker login --username AWS --password-stdin $registry
    if ($LASTEXITCODE -ne 0) { Write-Host "ECR login FAILED" -ForegroundColor Red; exit 1 }

    $fullTag = "${ECR}:${Tag}"
    docker tag "osu-pipeline:$Tag" $fullTag
    Write-Host "Pushing $fullTag ..." -ForegroundColor Cyan
    docker push $fullTag
    if ($LASTEXITCODE -ne 0) { Write-Host "docker push FAILED" -ForegroundColor Red; exit 1 }
    Write-Host "Pushed: $fullTag" -ForegroundColor Green
}

Write-Host "`nDone." -ForegroundColor Cyan
