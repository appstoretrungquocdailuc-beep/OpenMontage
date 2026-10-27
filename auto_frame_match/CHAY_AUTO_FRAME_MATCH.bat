@echo off
chcp 65001 >nul
title AUTO FRAME MATCH 2K
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" call CAI_DAT.bat
".venv\Scripts\python.exe" -m afm.gui
if errorlevel 1 (
  echo.
  echo Chuong trinh dung voi loi. Xem log trong thu muc logs\
  pause
)
