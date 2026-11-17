@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
echo ================================================================
echo   BENCH V9.9 - QUET RANGE-TURBO (LIVE, cung link moi vong, SHA-256)
echo   Moi buoc tai cung 1 video nhieu lan: chay TUNG buoc, nghi vai phut giua cac buoc.
echo   Gap 429/403 lap lai: bench TU DUNG.
echo ================================================================
if not exist urls.txt ( echo [!] Chua co urls.txt & pause & exit /b 1 )

echo.
echo [1/5] So ket noi video: 32 / 48 / 64 / 80
python bench_v99.py live_dl --urls urls.txt --engines B --reps 3 --gap 3 --b-variants preset:v99_conns
if errorlevel 3 goto stopped
echo.
echo [2/5] So worker audio: 8 / 16 / 24 / 32
python bench_v99.py live_dl --urls urls.txt --engines B --reps 3 --gap 3 --b-variants preset:v99_audio
if errorlevel 3 goto stopped
echo.
echo [3/5] Hedge toi da: 0 / 8 / 16 / 40
python bench_v99.py live_dl --urls urls.txt --engines B --reps 3 --gap 3 --b-variants preset:v99_hedge
if errorlevel 3 goto stopped
echo.
echo [4/5] Kich thuoc range: tu dong / 1 / 2 / 4 MiB
python bench_v99.py live_dl --urls urls.txt --engines B --reps 3 --gap 3 --b-variants preset:v99_range
if errorlevel 3 goto stopped
echo.
echo [5/5] A (V9.8.1) / B (ung vien) / tail-rescue bat-tat / HTTP/2 (httpx) / ket noi giu nong
python bench_v99.py live_dl --urls urls.txt --engines A,B,D --h2-n 16 --reps 3 --gap 3 --b-variants preset:v99_rescue
if errorlevel 3 goto stopped
python bench_v99.py live_dl --urls urls.txt --engines A,B --reps 3 --gap 3 --warm --b-variants preset:v99_rescue
goto done
:stopped
echo [!] Bench dung som (429/403). Doi 15-30 phut roi chay lai; KHONG chay lien tuc.
:done
echo Xong. Gui lai perf_report\v99_live_dl_*_summary.md va .csv
pause
