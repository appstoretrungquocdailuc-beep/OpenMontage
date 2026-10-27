@echo off
chcp 65001 >nul
title AUTO FRAME MATCH 2K - Cai dat
cd /d "%~dp0"
echo ============================================================
echo   AUTO FRAME MATCH 2K - CAI DAT LAN DAU
echo ============================================================
set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY (where python >nul 2>nul && set "PY=python")
if not defined PY (
  echo [LOI] Chua cai Python 3.10 tro len.
  echo Tai tai https://www.python.org/downloads/  ^(tick "Add python.exe to PATH"^)
  pause
  exit /b 1
)
%PY% --version
if not exist ".venv\Scripts\python.exe" (
  echo Dang tao moi truong ao .venv ...
  %PY% -m venv .venv || (echo [LOI] Khong tao duoc .venv & pause & exit /b 1)
)
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -r requirements.txt || (echo [LOI] Cai thu vien that bai & pause & exit /b 1)
echo.
set "FF="
if exist "bin\ffmpeg.exe" set "FF=bin\ffmpeg.exe"
if not defined FF (where ffmpeg >nul 2>nul && set "FF=ffmpeg")
if not defined FF (
  echo [CANH BAO] Chua tim thay FFmpeg.
  echo  Cach 1: mo PowerShell va chay:  winget install Gyan.FFmpeg
  echo  Cach 2: tai ban "ffmpeg-release-full" tai https://www.gyan.dev/ffmpeg/builds/
  echo          giai nen, chep ffmpeg.exe va ffprobe.exe vao thu muc  %~dp0bin
) else (
  echo FFmpeg: %FF%
  %FF% -hide_banner -encoders 2>nul | findstr /i "h264_nvenc" >nul && echo   + Co H.264 NVENC ^(GPU NVIDIA^) || echo   - Khong thay NVENC: se dung x264 ^(CPU^)
)
echo.
echo CAI DAT XONG. Nhap dup CHAY_AUTO_FRAME_MATCH.bat de chay.
pause
