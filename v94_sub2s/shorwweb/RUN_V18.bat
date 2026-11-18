@echo off
chcp 65001 >nul
cd /d "%~dp0"
python shorwweb_v18_fast360.py
if errorlevel 1 pause
