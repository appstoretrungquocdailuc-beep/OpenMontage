@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist config.json copy config.example.json config.json >nul
set /p VID=Nhap ID 1 video YouTube cong khai de test tai (vd dQw4w9WgXcQ): 
python YOUTUBE_WEBSUB_INSTANT_360P.py --selftest --video %VID%
echo.
echo Ket qua: TEST_REPORT_MAY_BAN.md
pause
