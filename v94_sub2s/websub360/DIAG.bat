@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist config.json copy config.example.json config.json >nul
echo [1] Chan doan nhanh (tool chinh van chay duoc)
echo [2] Chan doan + DANG KY LAI ca danh sach (phai TAT tool chinh, BAT ngrok)
echo [3] KIEM TRA SAU HUB: dang ky DONG BO + callback thu (phai TAT tool chinh, BAT ngrok)
set /p M=Chon 1, 2 hoac 3: 
if "%M%"=="3" (python YOUTUBE_WEBSUB_INSTANT_360P.py --hubtest ^& echo. ^& echo Ket qua: HUBTEST_REPORT.md ^& pause ^& exit /b)
if "%M%"=="2" (python YOUTUBE_WEBSUB_INSTANT_360P.py --diag --resubscribe) else (python YOUTUBE_WEBSUB_INSTANT_360P.py --diag)
echo.
echo Ket qua: DIAG_REPORT.md
pause
