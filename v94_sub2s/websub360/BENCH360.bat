@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist config.json copy config.example.json config.json >nul
set /p IDS=Nhap ID hoac link 1-2 video Shorts (cach nhau dau cach): 
python YOUTUBE_WEBSUB_INSTANT_360P.py --bench360 %IDS% --reps 10
echo.
echo Ket qua chi tiet: BENCH360.csv
pause
