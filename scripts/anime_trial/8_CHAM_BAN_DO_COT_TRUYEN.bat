@echo off
chcp 65001 >nul
set PYTHONUTF8=1
cd /d "%~dp0..\.."
if not exist ".venv-qwen\Scripts\python.exe" (
  echo [LOI] Chua cai Qwen3-VL. Hay chay scripts\anime_trial\6_CAI_DAT_QWEN3_VL.bat truoc.
  pause
  exit /b 1
)
echo ==== Cham ban do cot truyen tu kiem_tra_ban_do_cot_truyen.csv ====
if "%~1"=="" (
  ".venv-qwen\Scripts\python.exe" -m lib.anime_story_reasoner score
) else (
  ".venv-qwen\Scripts\python.exe" -m lib.anime_story_reasoner score --run "%~1"
)
pause
