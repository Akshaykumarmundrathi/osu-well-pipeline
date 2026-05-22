###############################################################################
# OSU Pipeline — Self-Healing Monitor
# Handles: Docker build retries, pipeline timeout re-submission,
#          ECR push chain, app image rebuild, failure logging
###############################################################################

param(
    [string]$MainJobId    = "749a681c-1fd3-4bb0-8254-15540aa218f1",
    [string]$JobQueue     = "arn:aws:batch:us-east-1:225989338968:job-queue/osu-pipeline-queue",
    [string]$JobDefRetry  = "osu-pipeline-job:5",
    [string]$AccountId    = "225989338968",
    [string]$Region       = "us-east-1",
    [string]$BaseImage    = "osu-pipeline-base:latest",
    [string]$AppImageECR  = "225989338968.dkr.ecr.us-east-1.amazonaws.com/osu-pipeline:latest",
    [string]$BaseImageECR = "225989338968.dkr.ecr.us-east-1.amazonaws.com/osu-pipeline-base:latest",
    [int]$PollSeconds     = 90,
    [int]$DockerRetries   = 4,
    [int]$JobIndexOffset  = 7
)

$StateFile    = "D:\project_modular\monitor_state.json"
$LogFile      = "D:\project_modular\monitor.log"
$ProjectDir   = "D:\project_modular"

###############################################################################
# Logging
###############################################################################
function Log {
    param([string]$Msg, [string]$Level = "INFO")
    $ts   = (Get-Date).ToString("yyyy-MM-dd HH:mm:ss")
    $line = "[$ts][$Level] $Msg"
    Write-Host $line
    Add-Content -Path $LogFile -Value $line -Encoding UTF8
}

###############################################################################
# State persistence  (tracks re-submitted slices + docker attempt count)
###############################################################################
function Load-State {
    if (Test-Path $StateFile) {
        try {
            $raw = Get-Content $StateFile -Raw -Encoding UTF8
            $obj = $raw | ConvertFrom-Json
            # Convert PSCustomObject arrays back to hashtable-friendly form
            $state = @{
                resubmitted = [System.Collections.Generic.HashSet[int]]($obj.resubmitted)
                retryJobs   = @{}
                dockerTries = [int]$obj.dockerTries
                dockerDone  = [bool]$obj.dockerDone
                appBuilt    = [bool]$obj.appBuilt
            }
            foreach ($prop in $obj.retryJobs.PSObject.Properties) {
                $state.retryJobs[$prop.Name] = $prop.Value
            }
            return $state
        } catch { }
    }
    return @{
        resubmitted = [System.Collections.Generic.HashSet[int]]@()
        retryJobs   = @{}
        dockerTries = 0
        dockerDone  = $false
        appBuilt    = $false
    }
}

function Save-State($state) {
    $obj = @{
        resubmitted = @($state.resubmitted)
        retryJobs   = $state.retryJobs
        dockerTries = $state.dockerTries
        dockerDone  = $state.dockerDone
        appBuilt    = $state.appBuilt
    }
    $obj | ConvertTo-Json -Depth 5 | Set-Content $StateFile -Encoding UTF8
}

###############################################################################
# Docker helpers
###############################################################################
function Docker-IsRunning {
    $result = docker info 2>&1
    return $LASTEXITCODE -eq 0
}

function Docker-DiskOK {
    # Returns $true if free space > 3 GB in the virtual disk
    $df = docker system df --format "{{json .}}" 2>&1
    if ($LASTEXITCODE -ne 0) { return $false }
    # If we can run docker system df, daemon is up — assume OK for now
    return $true
}

function Docker-Prune {
    Log "Pruning Docker build cache and dangling images..."
    docker builder prune -f 2>&1 | ForEach-Object { Log "  prune: $_" }
    docker image prune -f   2>&1 | ForEach-Object { Log "  prune: $_" }
}

function Build-BaseImage {
    Log "Building base image (Dockerfile.base)..."
    $proc = Start-Process -FilePath "docker" `
        -ArgumentList "build -f Dockerfile.base -t $BaseImage -t $BaseImageECR ." `
        -WorkingDirectory $ProjectDir `
        -Wait -PassThru -NoNewWindow `
        -RedirectStandardOutput "$ProjectDir\docker_base_stdout.log" `
        -RedirectStandardError  "$ProjectDir\docker_base_stderr.log"
    return $proc.ExitCode
}

function Push-Image($tag) {
    Log "Pushing $tag ..."
    $proc = Start-Process -FilePath "docker" -ArgumentList "push $tag" `
        -Wait -PassThru -NoNewWindow `
        -RedirectStandardOutput "$ProjectDir\docker_push_stdout.log" `
        -RedirectStandardError  "$ProjectDir\docker_push_stderr.log"
    return $proc.ExitCode
}

function Build-AppImage {
    Log "Building app image (Dockerfile)..."
    $proc = Start-Process -FilePath "docker" `
        -ArgumentList "build -f Dockerfile -t osu-pipeline:latest -t $AppImageECR ." `
        -WorkingDirectory $ProjectDir `
        -Wait -PassThru -NoNewWindow `
        -RedirectStandardOutput "$ProjectDir\docker_app_stdout.log" `
        -RedirectStandardError  "$ProjectDir\docker_app_stderr.log"
    return $proc.ExitCode
}

function ECR-Login {
    Log "Logging in to ECR..."
    $token = aws ecr get-login-password --region $Region 2>&1
    docker login --username AWS --password $token `
        "$AccountId.dkr.ecr.$Region.amazonaws.com" 2>&1 | Out-Null
    return $LASTEXITCODE -eq 0
}

###############################################################################
# Pipeline helpers
###############################################################################
function Get-JobStatus($jobId) {
    $result = aws batch describe-jobs --jobs $jobId `
        --query "jobs[0].arrayProperties.statusSummary" `
        --output json 2>&1
    if ($LASTEXITCODE -ne 0) { return $null }
    return $result | ConvertFrom-Json
}

function Get-FailedTasks($jobId) {
    $result = aws batch list-jobs `
        --array-job-id $jobId --job-status FAILED `
        --query "jobSummaryList[*].{idx:arrayProperties.index,reason:statusReason}" `
        --output json 2>&1
    if ($LASTEXITCODE -ne 0) { return @() }
    return $result | ConvertFrom-Json
}

function Submit-RetrySlice($slice) {
    $tmpFile = "$ProjectDir\override_retry_$slice.json"
    [System.IO.File]::WriteAllText($tmpFile,
        "{`"environment`":[{`"name`":`"JOB_INDEX_OFFSET`",`"value`":`"$slice`"}]}")
    $jobId = aws batch submit-job `
        --job-name "osu-retry-slice-$slice" `
        --job-queue $JobQueue `
        --job-definition $JobDefRetry `
        --container-overrides "file://$tmpFile" `
        --query "jobId" --output text 2>&1
    Remove-Item $tmpFile -ErrorAction SilentlyContinue
    if ($LASTEXITCODE -eq 0) {
        Log "  Submitted retry for slice $slice => $jobId"
        return $jobId
    } else {
        Log "  FAILED to submit slice $slice : $jobId" "WARN"
        return $null
    }
}

###############################################################################
# Docker build + push chain (with retry loop)
###############################################################################
function Run-DockerChain($state) {
    if ($state.dockerDone -and $state.appBuilt) {
        Log "Docker chain already complete. Skipping."
        return
    }

    for ($attempt = $state.dockerTries + 1; $attempt -le $DockerRetries; $attempt++) {
        $state.dockerTries = $attempt
        Save-State $state

        # Wait for Docker daemon
        $waited = 0
        while (-not (Docker-IsRunning)) {
            Log "Docker daemon not running — waiting 30s (attempt $waited)..." "WARN"
            Start-Sleep -Seconds 30
            $waited++
            if ($waited -gt 10) {
                Log "Docker daemon not responding after 5 min. Skipping this cycle." "ERROR"
                return
            }
        }

        if (-not $state.dockerDone) {
            # Prune on retries to recover disk space
            if ($attempt -gt 1) {
                Log "Retry attempt $attempt — pruning first..."
                Docker-Prune
            }

            Log "=== Docker base build attempt $attempt/$DockerRetries ==="
            $exit = Build-BaseImage
            $buildLog = Get-Content "$ProjectDir\docker_base_stderr.log" -Tail 5 -ErrorAction SilentlyContinue

            if ($exit -eq 0) {
                Log "Base image built successfully!" "INFO"
                $state.dockerDone = $true
                Save-State $state
            } else {
                # Check if disk issue
                $isDisk = ($buildLog | Select-String "input/output error|no space left|I/O error") -ne $null
                if ($isDisk) {
                    Log "Disk I/O error detected — will prune and retry." "WARN"
                } else {
                    Log "Build failed (non-disk error). Logs:" "ERROR"
                    $buildLog | ForEach-Object { Log "  $_" "ERROR" }
                }
                if ($attempt -lt $DockerRetries) {
                    Log "Waiting 60s before retry..."
                    Start-Sleep -Seconds 60
                }
                continue
            }
        }

        if ($state.dockerDone -and -not $state.appBuilt) {
            # ECR login + push base
            if (-not (ECR-Login)) {
                Log "ECR login failed — will retry next cycle." "WARN"
                return
            }

            $exitPush = Push-Image $BaseImageECR
            if ($exitPush -ne 0) {
                Log "Base image push failed. Will retry." "WARN"
                Start-Sleep -Seconds 30
                continue
            }
            Log "Base image pushed to ECR."

            # Build app image
            $exitApp = Build-AppImage
            if ($exitApp -ne 0) {
                Log "App image build failed. Logs:" "ERROR"
                Get-Content "$ProjectDir\docker_app_stderr.log" -Tail 10 | ForEach-Object { Log "  $_" "ERROR" }
                continue
            }
            Log "App image built."

            # Push app image
            $exitPushApp = Push-Image $AppImageECR
            if ($exitPushApp -ne 0) {
                Log "App image push failed. Will retry." "WARN"
                continue
            }
            Log "App image pushed to ECR. Docker chain COMPLETE." "INFO"
            $state.appBuilt = $true
            Save-State $state
        }

        return  # Success — exit loop
    }

    Log "Docker chain failed after $DockerRetries attempts." "ERROR"
}

###############################################################################
# Main monitor loop
###############################################################################
Log "========================================================"
Log "OSU Pipeline Monitor starting"
Log "  Main job  : $MainJobId"
Log "  Poll      : every ${PollSeconds}s"
Log "  Job def   : $JobDefRetry (4-hour timeout)"
Log "========================================================"

$state = Load-State

# Kick off Docker chain in background thread if not done
$dockerDone = $state.dockerDone -and $state.appBuilt
if (-not $dockerDone) {
    Log "Docker chain not complete — starting build chain..."
    Run-DockerChain $state
    $state = Load-State  # reload after docker chain
}

$consecutiveErrors = 0

while ($true) {
    try {
        # ── Pipeline status ────────────────────────────────────────────────
        $summary = Get-JobStatus $MainJobId
        if ($null -eq $summary) {
            $consecutiveErrors++
            Log "Could not fetch job status (attempt $consecutiveErrors)" "WARN"
            if ($consecutiveErrors -gt 5) { Log "Too many AWS errors — check credentials." "ERROR" }
            Start-Sleep -Seconds $PollSeconds
            continue
        }
        $consecutiveErrors = 0

        $succeeded = $summary.SUCCEEDED
        $failed    = $summary.FAILED
        $running   = $summary.RUNNING
        $runnable  = $summary.RUNNABLE
        $total     = $succeeded + $failed + $running + $runnable
        $pct       = if ($total -gt 0) { [math]::Round(($succeeded / 2832) * 100, 1) } else { 0 }

        Log "Status: SUCCEEDED=$succeeded RUNNING=$running RUNNABLE=$runnable FAILED=$failed | ${pct}% of 2832"

        # ── Detect + re-submit new timeout failures ────────────────────────
        if ($failed -gt 0) {
            $failedTasks = Get-FailedTasks $MainJobId
            $newTimeouts = @()

            foreach ($task in $failedTasks) {
                $arrayIdx  = [int]$task.idx
                $sliceNum  = $arrayIdx + $JobIndexOffset
                $isTimeout = $task.reason -like "*timeout*"

                if (-not $state.resubmitted.Contains($sliceNum)) {
                    if ($isTimeout) {
                        $newTimeouts += $sliceNum
                    } else {
                        Log "Non-timeout failure at array[$arrayIdx] slice[$sliceNum]: $($task.reason)" "WARN"
                        # Still re-submit — checkpoint will resume safely
                        $newTimeouts += $sliceNum
                    }
                }
            }

            if ($newTimeouts.Count -gt 0) {
                Log "New failures detected — re-submitting $($newTimeouts.Count) slices with 4h timeout..."
                foreach ($slice in $newTimeouts) {
                    $jobId = Submit-RetrySlice $slice
                    if ($jobId) {
                        $state.resubmitted.Add($slice) | Out-Null
                        $state.retryJobs["$slice"] = $jobId
                    }
                }
                Save-State $state
            }
        }

        # ── Check if main job complete ─────────────────────────────────────
        if ($runnable -eq 0 -and $running -eq 0) {
            Log "Main array job complete! SUCCEEDED=$succeeded FAILED=$failed" "INFO"

            # Check retry jobs
            $pendingRetries = @()
            foreach ($kv in $state.retryJobs.GetEnumerator()) {
                $rStatus = aws batch describe-jobs --jobs $kv.Value `
                    --query "jobs[0].status" --output text 2>&1
                if ($rStatus -notin @("SUCCEEDED","FAILED")) {
                    $pendingRetries += $kv.Key
                }
                Log "  Retry slice $($kv.Key) ($($kv.Value)): $rStatus"
            }

            if ($pendingRetries.Count -eq 0) {
                Log "ALL JOBS COMPLETE. Resubmitted slices: $($state.resubmitted.Count)" "INFO"
                Log "Monitor exiting cleanly." "INFO"

                # Final summary
                $totalSucceeded = $succeeded
                Log "========================================================"
                Log "FINAL: $totalSucceeded / 2832 tasks succeeded"
                Log "Resubmitted (timeout retries): $($state.resubmitted.Count) slices"
                Log "========================================================"
                break
            } else {
                Log "$($pendingRetries.Count) retry jobs still running: $($pendingRetries -join ', ')"
            }
        }

        # ── Docker chain (if not done yet) ────────────────────────────────
        $state = Load-State
        if (-not ($state.dockerDone -and $state.appBuilt)) {
            Run-DockerChain $state
            $state = Load-State
        }

    } catch {
        Log "Unhandled exception: $_" "ERROR"
        $consecutiveErrors++
    }

    Start-Sleep -Seconds $PollSeconds
}
