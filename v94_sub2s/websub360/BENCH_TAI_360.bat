@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist config.json copy config.example.json config.json >nul
echo ============================================================
echo  BENCH TAI 360P THAT - so sanh cac phuong an mang/tai
echo  Moi luot lay link MOI va tai lai tu dau (khong dung file co san).
echo  Nen dung 3-4 video Shorts khac dung luong (vd 3 MB, 6 MB, 9 MB).
echo  Tat tool WS360 dang chay truoc khi do (de mang khong bi chia).
echo ============================================================
set /p IDS=Nhap 3-4 ID hoac link video (cach nhau dau cach): 
python -m pip install -q "httpx[http2]" 2>nul
python YOUTUBE_WEBSUB_INSTANT_360P.py --bench-dl %IDS%
echo.
echo Ket qua: BENCH_DL.md (bang tom tat) va BENCH_DL.csv (tung luot). Gui 2 file nay de phan tich.
pause
