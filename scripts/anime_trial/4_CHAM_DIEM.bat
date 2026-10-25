@echo off
chcp 65001 >nul
set PYTHONUTF8=1
cd /d "%~dp0..\.."
if not exist ".venv-anime\Scripts\python.exe" (
  echo [LOI] Chua cai dat. Hay chay scripts\anime_trial\1_CAI_DAT.bat truoc.
  pause
  exit /b 1
)
echo ==== Cham diem tu 2 file CSV da dien ====
if "%~1"=="" (
  ".venv-anime\Scripts\python.exe" -m lib.anime_trial score
) else (
  ".venv-anime\Scripts\python.exe" -m lib.anime_trial score --run "%~1"
)
pause
