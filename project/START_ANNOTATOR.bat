@echo off
cd /d D:\project_modular\project
echo Campaigns: c8_layout (477) / c12_modern (165) / early30s_loc (194) / c7_grid (143) / c6_county (126)
set /p CAMP="campaign [c8_layout]: "
if "%CAMP%"=="" set CAMP=c8_layout
python annotate_campaigns.py --campaign %CAMP%
pause
