@echo off
REM Self-healing sequential runner v2 — fresh logs per session so stale
REM completion strings can't fool the checks.
cd /d D:\project_modular\project
set SESSION=%RANDOM%
start "keepawake" /min powershell -WindowStyle Hidden -Command "$s=Add-Type -MemberDefinition '[DllImport(\"kernel32.dll\")] public static extern uint SetThreadExecutionState(uint esFlags);' -Name P -PassThru; while($true){ $s::SetThreadExecutionState(0x80000003); Start-Sleep 50 }"
:loop
if exist D:\project_outputs\CHAIN_DONE.marker goto done
set STATUS_SHARD_SUFFIX=redoD
set LOG1=D:\project_outputs\wd_%SESSION%_redo.log
python run_c2345.py --workers 1 --no-push > %LOG1% 2>&1
findstr /c:"run_c2345.py complete" %LOG1% >/dev/null 2>&1
if errorlevel 1 goto loop
set STATUS_SHARD_SUFFIX=regen
set LOG2=D:\project_outputs\wd_%SESSION%_regen.log
python main.py --index D:\project_outputs\dot_regen_index.csv --output D:\project_outputs --workers 1 > %LOG2% 2>&1
findstr /c:"Pipeline finished in" %LOG2% >/dev/null 2>&1
if errorlevel 1 goto loop
echo CHAIN_COMPLETE > D:\project_outputs\CHAIN_DONE.marker
:done
