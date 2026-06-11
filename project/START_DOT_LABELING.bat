@echo off
cd /d D:\project_modular\project
set OUTPUT_ROOT=D:\project_outputs_test1000
echo Labeling dot positions on FAILED grids from the C6-C13 exposure run.
echo Left-click=dot  N=no dot visible  G=not a grid  Space=save  Q=quit
python inspect_dots.py --form-type any --status failed --limit 300
pause
