@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist config.json copy config.example.json config.json >nul
echo ============================================================
echo  BENCH ARIA2 RPC vs downloader WS360 - CUNG URL, CUNG MAY
echo  Moi vong: lay 1 link MOI roi tai CHINH link do bang:
echo    ws360 (hien tai) + aria2 RPC 1 / 4 / 8 / 12 ket noi
echo  Kiem tra moi file: dung dung luong + MP4 + ffprobe + SHA-256 trung nhau.
echo  Can aria2c.exe canh tool (hoac thu muc bin\ / PATH) - giong 360pok.
echo  KHONG quet, KHONG ghi Excel, KHONG dang TikTok. Gap HTTP 429 se tu dung.
echo  Nen dung 3-4 video 8-10 MB (Shorts) va 1 video dai.
echo ============================================================
set /p IDS=Nhap 3-4 ID hoac link video (cach nhau dau cach): 
python YOUTUBE_WEBSUB_INSTANT_360P.py --bench-aria2 %IDS% --conns 1,4,8,12
echo.
echo Ket qua: BENCH_ARIA2.md, BENCH_ARIA2_SUMMARY.md (tu ket luan), BENCH_ARIA2.csv. Gui 3 file nay de phan tich.
pause
