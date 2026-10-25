@echo off
chcp 65001 >nul
set PYTHONUTF8=1
cd /d "%~dp0..\.."
echo ==== OpenMontage - cai dat bo chay thu anime ====
if exist ".venv-anime\Scripts\python.exe" goto have_venv
set "PYCMD="
py -3.11 -c "import sys" >nul 2>&1 && set "PYCMD=py -3.11"
if not defined PYCMD py -3.12 -c "import sys" >nul 2>&1 && set "PYCMD=py -3.12"
if not defined PYCMD python -c "import sys; assert (3,9) <= sys.version_info[:2] <= (3,12)" >nul 2>&1 && set "PYCMD=python"
if not defined PYCMD (
  echo [LOI] Khong tim thay Python 3.11 hoac 3.12.
  echo Tai Python 3.11 tai https://www.python.org/downloads/windows/ va tick "Add python.exe to PATH".
  pause
  exit /b 1
)
echo Tao moi truong ao .venv-anime bang %PYCMD% ...
%PYCMD% -m venv .venv-anime
if errorlevel 1 (
  echo [LOI] Khong tao duoc .venv-anime
  pause
  exit /b 1
)
:have_venv
".venv-anime\Scripts\python.exe" -m pip install --upgrade pip
where ffmpeg >nul 2>&1
if errorlevel 1 (
  echo [CANH BAO] Chua co FFmpeg trong PATH.
  choice /M "Cai FFmpeg mien phi bang winget - Gyan.FFmpeg - ngay bay gio"
  if errorlevel 2 goto skip_ffmpeg
  winget install --id Gyan.FFmpeg -e --accept-source-agreements --accept-package-agreements
  echo Da cai FFmpeg. DONG cua so nay roi chay lai 1_CAI_DAT.bat de PATH moi co hieu luc.
  pause
  exit /b 0
)
:skip_ffmpeg
".venv-anime\Scripts\python.exe" -m lib.anime_trial setup %*
if errorlevel 1 (
  echo.
  echo [CHUA XONG] Co buoc loi - doc cac dong FAIL / LOI o tren, xem muc "Xu ly loi" trong huong dan.
) else (
  echo.
  echo [OK] Cai dat xong. Dat video vao anime_trial\video_vao roi chay 2_CHAY_THU_DOAN_NGAN.bat
)
pause
