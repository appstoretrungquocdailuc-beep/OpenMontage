@echo off
chcp 65001 >nul
set PYTHONUTF8=1
cd /d "%~dp0..\.."
if not exist ".venv-anime\Scripts\python.exe" (
  echo [LOI] Chua cai dat. Hay chay scripts\anime_trial\1_CAI_DAT.bat truoc.
  pause
  exit /b 1
)
echo ==== Kiem tra lai thoai tu transcript da luu - khong chay lai Whisper ====
if "%~1"=="" (
  ".venv-anime\Scripts\python.exe" -m lib.anime_trial recheck
) else (
  ".venv-anime\Scripts\python.exe" -m lib.anime_trial recheck --run "%~1"
)
pause
