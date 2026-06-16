@echo off
cd /d D:\project_modular\project
set STATUS_SHARD_SUFFIX=c13
set IDX=D:\project_outputs_sample\c13_index.csv
set LOG=D:\project_outputs_sample\chain_c13.log
echo chain c13 start %DATE% %TIME% > %LOG%
:loop
python next_chunk.py --index %IDX% --shard c13 --chunk 50 --deadline-seconds 25000 >> %LOG% 2>&1
if errorlevel 3 goto done
del D:\project_outputs\*.lock 2>nul
python main.py --index D:\project_outputs\_chunks\chunk.csv --output D:\project_outputs --workers 1 >> %LOG% 2>&1
goto loop
:done
echo CHAIN DONE %DATE% %TIME% >> %LOG%
echo done > D:\project_outputs\C13_DONE.marker
