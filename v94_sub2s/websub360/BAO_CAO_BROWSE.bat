@echo off
chcp 65001 >nul
cd /d "%~dp0"
python YOUTUBE_WEBSUB_INSTANT_360P.py --browse-report
if exist BROWSE_REPORT.md start "" notepad BROWSE_REPORT.md
pause
