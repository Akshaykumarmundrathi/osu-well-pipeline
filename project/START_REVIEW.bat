@echo off
cd /d D:\project_modular\project
set OUTPUT_ROOT=D:\project_outputs
echo ============================================================
echo  RECORD REVIEW CONSOLE  -  ALREADY-PROCESSED records
echo  (539 records, 1%% per month, spread across all eras)
echo ------------------------------------------------------------
echo  1=grid 2=county 3=STR 4=latlong  - drag on page to draw box
echo  PageUp/Dn=page  S=same as prev  C=clear
echo  O=OK  W=WRONG  Left/Right=move record
echo  Format box: type top_left / bottom_left / latlong / etc.
echo  Saved to D:\project_outputs\review_notes.csv (resumable)
echo ============================================================
python review_console.py --index D:\project_outputs_sample\review_processed_index.csv
pause
