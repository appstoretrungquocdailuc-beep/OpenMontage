@echo off
chcp 65001 >nul
cd /d "%~dp0"
rem So sanh CUNG video: V39 goc (doc scanner_ledger.jsonl) va WS360 + BROWSE (doc database.sqlite). CHI DOC, khong ghi gi.
set "V39DIR=%~1"
if "%V39DIR%"=="" set /p V39DIR=Nhap thu muc V39 (chua scanner_ledger.jsonl), vd C:\V39_SO_SANH : 
python YOUTUBE_WEBSUB_INSTANT_360P.py --browse-compare "%V39DIR%"
if exist BROWSE_COMPARE.md start "" notepad BROWSE_COMPARE.md
pause
