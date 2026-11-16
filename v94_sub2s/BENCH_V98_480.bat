@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
echo ================================================================
echo   BENCH V9.8 - 480p (video 244 + audio JA)  - ket qua: perf_report\
echo   Gap 429 hoac 403 lap lai: bench TU DUNG (khong don request).
echo ================================================================
if not exist urls.txt (
  echo [!] Chua co urls.txt. Tao file urls.txt, moi dong 1 URL YouTube (video 40-60 MB o 480p la tot nhat^).
  echo     Van chay buoc 1 (MOCK, khong can mang^).
)

echo.
echo [1/4] MOCK tren may nay (khong can mang): chi phi ghi dia / khoa / prealloc THAT cua Windows
python bench_v98.py mock480 --engines A,B --scenarios S1_perconn,S2_linkcap --reps 4 --b-variants preset:flags
if not exist urls.txt goto done

echo.
echo [2/4] LINKCAP: toc do toi da duong truyen + CDN (chi video, 20/32/40 ket noi) - gioi han vat ly
python bench_v98.py linkcap --urls urls.txt --reps 2 --gap 4
if errorlevel 3 goto stopped

echo.
echo [3/4] LIVE_DL: cung 1 link moi vong, A=V9.7.1  B=V9.8 (4 bien the)  C=aria2 RPC 8/16 - SHA-256 + ffprobe
python bench_v98.py live_dl --urls urls.txt --engines A,B,C --aria2-split 8,16 --reps 3 --gap 3 --b-variants preset:live
if errorlevel 3 goto stopped

echo.
echo [4/4] LIVE480: NGUYEN pipeline (resolve - tai - DIRECT-CUT/MUX); co/khong prewarm ffmpeg; FASTEST_VALID_480P
python bench_v98.py live480 --urls urls.txt --engines A,B --reps 3 --gap 4 --b-variants preset:pipeline
if errorlevel 3 goto stopped
goto done

:stopped
echo [!] Bench dung som (429/403). Doi 15-30 phut roi chay lai; KHONG chay lien tuc.

:done
echo.
echo Xong. Gui lai cac file perf_report\v98_*_summary.md va v98_*.csv (khong chua URL co chu ky).
pause
