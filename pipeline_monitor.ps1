$MainJobId   = "749a681c-1fd3-4bb0-8254-15540aa218f1"
$JobQueue    = "arn:aws:batch:us-east-1:225989338968:job-queue/osu-pipeline-queue"
$JobDef      = "osu-pipeline-job:5"
$Offset      = 7
$StateFile   = "D:\project_modular\monitor_state.json"
$LogFile     = "D:\project_modular\monitor.log"

function Log($msg, $lvl="INFO") {
    $line = "[$((Get-Date).ToString('HH:mm:ss'))][PLN][$lvl] $msg"
    Write-Host $line
    Add-Content $LogFile $line -Encoding UTF8
}

function Load-Resubmitted {
    if (Test-Path $StateFile) {
        $raw = Get-Content $StateFile -Raw -Encoding UTF8 | ConvertFrom-Json
        return [System.Collections.Generic.HashSet[int]]($raw.resubmitted)
    }
    return [System.Collections.Generic.HashSet[int]]@()
}

function Save-Resubmitted($set, $jobMap) {
    $existing = @{}
    if (Test-Path $StateFile) {
        $raw = Get-Content $StateFile -Raw -Encoding UTF8 | ConvertFrom-Json
        foreach ($p in $raw.retryJobs.PSObject.Properties) { $existing[$p.Name] = $p.Value }
        foreach ($k in $jobMap.Keys) { $existing[$k] = $jobMap[$k] }
        $raw.resubmitted = @($set)
        $raw.retryJobs   = $existing
        $raw | ConvertTo-Json -Depth 5 | Set-Content $StateFile -Encoding UTF8
    }
}

Log "Pipeline monitor started. Polling every 90s..."
$cycle = 0

while ($true) {
    $cycle++
    try {
        $summary = aws batch describe-jobs --jobs $MainJobId `
            --query "jobs[0].arrayProperties.statusSummary" --output json 2>&1 | ConvertFrom-Json

        $S=$summary.SUCCEEDED; $F=$summary.FAILED; $R=$summary.RUNNING; $Q=$summary.RUNNABLE
        $pct = [math]::Round(($S/2832)*100,1)
        Log "Cycle $cycle | S=$S R=$R Q=$Q F=$F | ${pct}%"

        if ($F -gt 0) {
            $resubmitted = Load-Resubmitted
            $failed = aws batch list-jobs --array-job-id $MainJobId --job-status FAILED `
                --query "jobSummaryList[*].{idx:arrayProperties.index,reason:statusReason}" `
                --output json 2>&1 | ConvertFrom-Json

            $newJobs = @{}
            foreach ($t in $failed) {
                $slice = [int]$t.idx + $Offset
                if ($resubmitted.Contains($slice)) { continue }

                $reason = $t.reason
                $tag    = if ($reason -like "*timeout*") { "TIMEOUT" } else { "CRASH" }
                Log "  [$tag] array[$($t.idx)] => slice $slice : $reason" "WARN"

                $tmp = "D:\project_modular\ov_$slice.json"
                [System.IO.File]::WriteAllText($tmp,
                    "{`"environment`":[{`"name`":`"JOB_INDEX_OFFSET`",`"value`":`"$slice`"}]}")
                $jid = aws batch submit-job --job-name "osu-retry-slice-$slice" `
                    --job-queue $JobQueue --job-definition $JobDef `
                    --container-overrides "file://$tmp" `
                    --query "jobId" --output text 2>&1
                Remove-Item $tmp -ErrorAction SilentlyContinue

                if ($LASTEXITCODE -eq 0) {
                    Log "  Resubmitted slice $slice => $jid"
                    $resubmitted.Add($slice) | Out-Null
                    $newJobs["$slice"] = $jid
                } else {
                    Log "  Submit FAILED for slice $slice : $jid" "ERROR"
                }
            }
            if ($newJobs.Count -gt 0) { Save-Resubmitted $resubmitted $newJobs }
        }

        # Done?
        if ($Q -eq 0 -and $R -eq 0) {
            Log "Main array job finished! S=$S F=$F" "INFO"
            break
        }
    } catch {
        Log "Exception: $_" "ERROR"
    }
    Start-Sleep -Seconds 90
}
Log "Pipeline monitor exiting."
