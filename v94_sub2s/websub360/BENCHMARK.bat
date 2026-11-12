@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist config.json copy config.example.json config.json >nul
set /p IDS=Nhap 2 ID video (1 ngan, 1 dai), cach nhau dau cach: 
python YOUTUBE_WEBSUB_INSTANT_360P.py --bench %IDS% --reps 3
echo.
echo Ket qua: BENCHMARK.csv
pause
