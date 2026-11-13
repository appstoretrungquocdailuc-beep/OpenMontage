@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist config.json copy config.example.json config.json >nul
echo ============================================================
echo  BENCH TOAN TUYEN (CHAY THU - KHONG BAM POST)
echo  tai 360p moi -^> giao TikTok -^> gan file -^> cho nut Post sang -^> DUNG
echo  event / lite / sparse xoay vong. Moi luot tai lai tu dau.
echo  Tat WS360 dang chay. Mo Genlogin. Dung 1 profile THU.
echo ============================================================
set /p PID=Profile ID Genlogin: 
set /p IDS=Nhap 2-3 ID hoac link video Shorts (cach nhau dau cach): 
set TAM_HIGH_PRIORITY=0
python YOUTUBE_WEBSUB_INSTANT_360P.py --bench-pipeline %IDS% --profile %PID%
echo.
echo Ket qua: BENCH_PIPELINE.md + BENCH_PIPELINE.csv  - gui 2 file nay.
pause
