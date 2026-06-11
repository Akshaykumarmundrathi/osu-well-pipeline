@echo off
cd /d D:\project_modular\project
set STATUS_SHARD_SUFFIX=redoD
python run_c2345.py --workers 2 --no-push > D:\project_outputs\run_redo_g.log 2> D:\project_outputs\run_redo_g_err.log
set STATUS_SHARD_SUFFIX=regen
python main.py --index D:\project_outputs\dot_regen_index.csv --output D:\project_outputs --workers 1 > D:\project_outputs\run_regen4.log 2> D:\project_outputs\run_regen4_err.log
echo CHAIN_COMPLETE > D:\project_outputs\CHAIN_DONE.marker
