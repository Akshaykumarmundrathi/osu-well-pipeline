@echo off
cd /d D:\project_modular\project
set STATUS_SHARD_SUFFIX=unseen6k
set IDX=D:\project_outputs_sample\unseen_index.csv
set LOG=D:\project_outputs_sample\chain.log
echo chain start %DATE% %TIME% > %LOG%
:loop
python next_chunk.py --index %IDX% --shard unseen6k --chunk 50 --deadline-seconds 17400 >> %LOG% 2>&1
if errorlevel 3 goto done
del D:\project_outputs\*.lock 2>nul
python main.py --index D:\project_outputs\_chunks\chunk.csv --output D:\project_outputs --workers 1 >> %LOG% 2>&1
goto loop
:done
echo CHAIN DONE %DATE% %TIME% >> %LOG%
echo done > D:\project_outputs\UNSEEN6K_DONE.marker
