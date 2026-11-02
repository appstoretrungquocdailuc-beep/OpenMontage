@echo off
chcp 65001 >nul
cd /d "%~dp0"
python V9_3_AUDIO.py
if errorlevel 1 pause
