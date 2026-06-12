@echo off
cd /d D:\project_modular\project
set OUTPUT_ROOT=D:\project_outputs
echo ============================================================
echo  C2 DOT LABELING (P11 - hollow-circle well marks, 1926-40)
echo ------------------------------------------------------------
echo  The U-Net was trained on filled ink dots and is BLIND to
echo  the hollow "o" circle marks on C2 forms. Your labels teach
echo  it the circle style.
echo.
echo  PREDICT-then-CORRECT:
echo    - ORANGE ring = model guess (usually absent here - that's
echo      the point). If it happens to be right, press SPACE.
echo    - LEFT-CLICK the center of the real hollow circle mark,
echo      then SPACE to save.
echo    - N = grid has NO well mark visible
echo    - G = this image is NOT a grid (other table)
echo    - S = skip   U = undo   Q = quit (progress saved)
echo.
echo  Aim for ~120-150 labels. Mix of dot-present and N/G is
echo  ideal - the negatives matter too.
echo ============================================================
echo.
python inspect_dots.py --collection 2 --form-type any --status failed --limit 300
pause
