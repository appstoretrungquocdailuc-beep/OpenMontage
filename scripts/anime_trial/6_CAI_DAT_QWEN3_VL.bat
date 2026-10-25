@echo off
chcp 65001 >nul
set PYTHONUTF8=1
cd /d "%~dp0..\.."
echo ==== Cai dat Qwen3-VL-4B-Thinking chay OFFLINE tren GPU - venv rieng .venv-qwen ====
echo Khong dung API tra phi. Can ~15 GB dia trong: thu vien CUDA ~3 GB + model ~9 GB.
echo Moi truong Whisper hien tai KHONG bi thay doi.
if exist ".venv-qwen\Scripts\python.exe" goto have_venv
set "PYCMD="
py -3.11 -c "import sys" >nul 2>&1 && set "PYCMD=py -3.11"
if not defined PYCMD py -3.12 -c "import sys" >nul 2>&1 && set "PYCMD=py -3.12"
if not defined PYCMD (
  echo [LOI] Khong tim thay Python 3.11 hoac 3.12 - cai tu python.org
  pause
  exit /b 1
)
%PYCMD% -m venv .venv-qwen
if errorlevel 1 (
  echo [LOI] Khong tao duoc .venv-qwen
  pause
  exit /b 1
)
:have_venv
".venv-qwen\Scripts\python.exe" -m lib.qwen_local_backend setup %*
if errorlevel 1 (
  echo.
  echo [CHUA XONG] Xem dong LOI o tren, va muc "Xu ly loi" trong docs\anime-recap\HUONG_DAN_TANG1_QWEN3_VL.md
) else (
  echo.
  echo [OK] Qwen3-VL da chay duoc tren GPU. Tiep theo: 7_QWEN3_VL_HIEU_PHIM.bat
)
pause
