@echo off
chcp 65001 >nul
title AUTO FRAME MATCH 2K - Kiem thu
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" call CAI_DAT.bat
echo Dang chay bo kiem thu voi video tong hop (khoang 10-20 phut)...
set "AFM_TEST_DIR=%~dp0test_data"
".venv\Scripts\python.exe" -m pytest tests -v
pause
