param([string]$IndicesFile = "D:\project_modular\pending_retry_indices.txt")

$JobQueue   = "arn:aws:batch:us-east-1:225989338968:job-queue/osu-pipeline-queue"
$JobDef     = "osu-pipeline-job:5"
$ProjectDir = "D:\project_modular"
$StateFile  = "$ProjectDir\monitor_state.json"
$LogFile    = "$ProjectDir\bulk_submit.log"

function Log($msg) {
    $line = "[$(Get-Date -Format 'HH:mm:ss')] $msg"
    Write-Host $line
    Add-Content $LogFile $line -Encoding UTF8
}

$indices = (Get-Content $IndicesFile -Raw).Trim() -split "," | ForEach-Object { [int]$_ }
Log "Starting bulk submit: $($indices.Count) indices"

$submitted  = @{}
$failed     = @()
$batchSize  = 50
$total      = $indices.Count

for ($i = 0; $i -lt $total; $i++) {
    $idx   = $indices[$i]
    $slice = $idx + 7

    $tmpFile = "$ProjectDir\override_retry_$slice.json"
    [System.IO.File]::WriteAllText($tmpFile, "{`"environment`":[{`"name`":`"JOB_INDEX_OFFSET`",`"value`":`"$slice`"}]}")

    $jobId = aws batch submit-job `
        --job-name "osu-rev5-$slice" `
        --job-queue $JobQueue `
        --job-definition $JobDef `
        --container-overrides "file://$tmpFile" `
        --query "jobId" --output text 2>&1
    Remove-Item $tmpFile -ErrorAction SilentlyContinue

    if ($LASTEXITCODE -eq 0) {
        $submitted["$slice"] = $jobId.Trim()
    } else {
        $failed += $slice
    }

    # Progress log every 50 jobs
    if (($i + 1) % $batchSize -eq 0) {
        Log "Progress: $($i+1)/$total submitted=$($submitted.Count) failed=$($failed.Count)"

        # Incremental state save every 50
        $raw = Get-Content $StateFile -Raw -Encoding UTF8 | ConvertFrom-Json
        $resubmitted = [System.Collections.Generic.List[int]]($raw.resubmitted)
        $retryJobs = @{}
        foreach ($prop in $raw.retryJobs.PSObject.Properties) { $retryJobs[$prop.Name] = $prop.Value }
        foreach ($kv in $submitted.GetEnumerator()) {
            if (-not $resubmitted.Contains([int]$kv.Key)) {
                $resubmitted.Add([int]$kv.Key) | Out-Null
            }
            $retryJobs[$kv.Key] = $kv.Value
        }
        $newState = [ordered]@{
            resubmitted = @($resubmitted)
            retryJobs   = $retryJobs
            dockerTries = [int]$raw.dockerTries
            dockerDone  = $true
            appBuilt    = $true
        }
        $newState | ConvertTo-Json -Depth 5 | Set-Content $StateFile -Encoding UTF8
        $submitted = @{}  # reset batch to avoid memory growth
    }
}

# Final state save
$raw = Get-Content $StateFile -Raw -Encoding UTF8 | ConvertFrom-Json
$resubmitted = [System.Collections.Generic.List[int]]($raw.resubmitted)
$retryJobs = @{}
foreach ($prop in $raw.retryJobs.PSObject.Properties) { $retryJobs[$prop.Name] = $prop.Value }
foreach ($kv in $submitted.GetEnumerator()) {
    if (-not $resubmitted.Contains([int]$kv.Key)) { $resubmitted.Add([int]$kv.Key) | Out-Null }
    $retryJobs[$kv.Key] = $kv.Value
}
$newState = [ordered]@{
    resubmitted = @($resubmitted)
    retryJobs   = $retryJobs
    dockerTries = [int]$raw.dockerTries
    dockerDone  = $true
    appBuilt    = $true
}
$newState | ConvertTo-Json -Depth 5 | Set-Content $StateFile -Encoding UTF8

Log "DONE. Total submitted: $($resubmitted.Count - 209) new + 209 existing. Failed: $($failed.Count)"
if ($failed.Count -gt 0) { Log "Failed slices: $($failed -join ',')" }
