@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo So sanh WebSub va API tren du lieu THAT (database.sqlite) ...
python YOUTUBE_WEBSUB_INSTANT_360P.py --race-report
if exist RACE_BENCHMARK.md start "" notepad RACE_BENCHMARK.md
pause
