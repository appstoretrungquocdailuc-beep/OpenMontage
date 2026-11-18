@echo off
chcp 65001 >nul
cd /d "%~dp0"
python shorwweb_v10fast.py
if errorlevel 1 pause
