@echo off
cd /d D:\project_modular\project
set OUTPUT_ROOT=D:\project_outputs
echo ============================================================
echo  RECORD REVIEW CONSOLE  (box annotation)
echo  1=grid 2=county 3=STR 4=latlong  - drag to draw a box
echo  PageUp/Dn=page  S=same as prev  C=clear  O=OK  W=WRONG
echo  Left/Right=move record  - saved to review_notes.csv
echo ============================================================
python review_console.py --index D:\project_outputs_sample\unseen_index.csv
pause
