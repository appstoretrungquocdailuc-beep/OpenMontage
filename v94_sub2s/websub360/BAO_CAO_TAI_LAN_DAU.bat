@echo off
chcp 65001 >nul
cd /d "%~dp0"
python YOUTUBE_WEBSUB_INSTANT_360P.py --first-dl-report
if exist FIRST_DL_REPORT.md start "" notepad FIRST_DL_REPORT.md
pause
