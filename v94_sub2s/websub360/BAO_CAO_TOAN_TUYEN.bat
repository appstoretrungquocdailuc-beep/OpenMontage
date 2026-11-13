@echo off
chcp 65001 >nul
cd /d "%~dp0"
python YOUTUBE_WEBSUB_INSTANT_360P.py --pipeline-report
if exist PIPELINE_REPORT.md start "" notepad PIPELINE_REPORT.md
pause
