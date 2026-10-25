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
echo 1 = Thu 1 doan ngan 20 giay - toi da 4 luot goi, khoang 3-6 phut (LAM BUOC NAY TRUOC)
echo 2 = Thu 2 doan 30 giay - toi da 8 luot goi, khoang 6-15 phut
echo 3 = Ca tap - CHI chay khi lua chon 1 da dat (co the mat 30-90 phut)
choice /C 123 /M "Chon"
if errorlevel 3 (
  set "OPTS=--window-seconds 40 --frames-per-window 12 --verify-limit 10"
) else if errorlevel 2 (
  set "OPTS=--max-windows 2 --window-seconds 30 --frames-per-window 10 --verify-limit 2 --max-calls 8"
) else (
  set "OPTS=--max-windows 1 --window-seconds 20 --frames-per-window 8 --verify-limit 1 --max-calls 4"
)
if "%~1"=="" (
  ".venv-qwen\Scripts\python.exe" -m lib.anime_story_reasoner run %OPTS%
) else (
  ".venv-qwen\Scripts\python.exe" -m lib.anime_story_reasoner run %OPTS% --run "%~1"
)
echo.
echo Ket qua: thu muc reasoner trong thu muc ket qua - doc ban_do_cot_truyen.md va qwen_trial_report.md
pause
