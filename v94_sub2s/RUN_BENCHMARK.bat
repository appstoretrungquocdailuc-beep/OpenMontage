@echo off
chcp 65001 >nul
cd /d "%~dp0"
REM 1) Test offline (khong can mang)
python -m pytest -q
REM 2) Benchmark offline engine + trigger (khong can mang)
python bench_v93.py offline --reps 5
python bench_v93.py trigger --reps 30 --rows 500
REM 3) Benchmark LIVE A/B V9.3 vs V9.4 (can urls.txt, moi dong 1 URL)
if exist urls.txt (
  python bench_v93.py live --urls urls.txt --reps 3 --gap 5
) else (
  echo [!] Chua co urls.txt - copy urls.example.txt thanh urls.txt va dien URL that de chay LIVE.
)
echo Ket qua: thu muc perf_report\
pause
