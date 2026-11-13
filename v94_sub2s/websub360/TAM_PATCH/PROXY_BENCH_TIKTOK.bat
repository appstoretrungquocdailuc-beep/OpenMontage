@echo off
chcp 65001 >nul
cd /d "%~dp0"
set TAM_HIGH_PRIORITY=0
python tam_INSTANT_POST.py --proxy-bench
if errorlevel 1 pause
