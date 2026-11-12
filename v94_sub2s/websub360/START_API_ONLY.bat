@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist config.json copy config.example.json config.json >nul
python YOUTUBE_WEBSUB_INSTANT_360P.py --mode API_ONLY
pause
