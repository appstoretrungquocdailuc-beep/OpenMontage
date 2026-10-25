@echo off
chcp 65001 >nul
set PYTHONUTF8=1
cd /d "%~dp0..\.."
if not exist ".venv-anime\Scripts\python.exe" (
  echo [LOI] Chua cai dat. Hay chay scripts\anime_trial\1_CAI_DAT.bat truoc.
  pause
  exit /b 1
)
echo ==== Chay thu doan ngan 45 giay ====
if "%~1"=="" (
  ".venv-anime\Scripts\python.exe" -m lib.anime_trial run --mode clip
) else (
  ".venv-anime\Scripts\python.exe" -m lib.anime_trial run --mode clip --video "%~1"
)
echo.
echo Ket qua nam trong anime_trial\ket_qua - mo file trial_report.md cua lan chay moi nhat.
pause
