@echo off
chcp 65001 >nul
set PYTHONUTF8=1
cd /d "%~dp0..\.."
if not exist ".venv-qwen\Scripts\python.exe" (
  echo [LOI] Chua cai Qwen3-VL. Hay chay scripts\anime_trial\6_CAI_DAT_QWEN3_VL.bat truoc.
  pause
  exit /b 1
)
echo ==== Tang 1: Qwen3-VL doc phim va suy luan cot truyen - OFFLINE ====
echo 1 = Chay thu 2 doan dau - nhanh, de kiem tra
echo 2 = Ca tap - co the mat 30-90 phut
choice /C 12 /M "Chon"
if errorlevel 2 (set "PILOT=") else (set "PILOT=--max-windows 2")
if "%~1"=="" (
  ".venv-qwen\Scripts\python.exe" -m lib.anime_story_reasoner run %PILOT%
) else (
  ".venv-qwen\Scripts\python.exe" -m lib.anime_story_reasoner run %PILOT% --run "%~1"
)
echo.
echo Ket qua: thu muc reasoner trong thu muc ket qua - doc ban_do_cot_truyen.md va qwen_trial_report.md
pause
