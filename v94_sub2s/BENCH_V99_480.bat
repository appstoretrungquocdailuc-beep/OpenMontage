@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
echo ================================================================
echo   BENCH V9.9 - SMART AUTO FASTEST 480p   (ket qua: perf_report\v99_*)
echo   A = V9.8.1 dang chay (V9_4_SUB2S.py, KHONG sua)   B = ban ung vien V9_9_CANDIDATE.py
echo   Gap 429: bench TU DUNG. Khong ghi URL co chu ky.
echo ================================================================

echo.
echo [1/5] ENV: phien ban yt-dlp ma TOOL that su dung (module Python) so voi yt-dlp.exe cua CMD
python bench_v99.py env

echo.
echo [2/5] AUTOPSY: doc telemetry DA CO trong perf_report (khong tai gi) - range nao lam audio/video xong cham
python bench_v99.py autopsy --last 12 --top 12

echo.
echo [3/5] CUTLAB (khong can mang): ffmpeg cua may nay cat VP9 / AV1 / H.264 co sach khong
python bench_v99.py cutlab --engine B

if not exist urls.txt (
  echo.
  echo [!] Chua co urls.txt - tao file urls.txt, moi dong 1 URL YouTube, roi chay lai de lam buoc 4-5.
  goto done
)

echo.
echo [4/5] FORMATS (LIVE, khong tai media): moi nguon thuc su tra ve format 480p / audio JA nao
python bench_v99.py formats --urls urls.txt

echo.
echo [5/5] LIVE480 theo to hop: NGUYEN pipeline cho 244+139 / 397+139 / 397+249 / 244+249 / 135+139
python bench_v99.py live480 --urls urls.txt --reps 3 --gap 4
if errorlevel 3 echo [!] Bench dung som (429). Doi 15-30 phut roi chay lai; KHONG chay lien tuc.

:done
echo.
echo Xong. Gui lai cac file perf_report\v99_*.md va v99_*.csv (khong chua URL co chu ky).
pause
