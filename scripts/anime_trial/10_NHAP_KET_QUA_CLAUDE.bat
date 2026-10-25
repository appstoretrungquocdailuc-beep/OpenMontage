@echo off
chcp 65001 >nul
set PYTHONUTF8=1
cd /d "%~dp0..\.."
set "PY=.venv-anime\Scripts\python.exe"
if not exist "%PY%" (
  echo [LOI] Chua cai dat. Hay chay scripts\anime_trial\1_CAI_DAT.bat truoc.
  pause
  exit /b 1
)
set "PKT="
if not "%~1"=="" set PKT=--packet "%~1"
echo ==== Nhap ket qua tu Claude Pro vao goi moi nhat - hoac keo tha thu muc goi len file nay ====
echo 1 = Nhap HIEU PHIM - dan cau tra loi cua Claude, may kiem tra bang chung
echo 2 = DUYET phan hieu phim - chi khi da doi chieu voi video
echo 3 = Nhap KICH BAN - may kiem tra va xuat kich_ban.md + cut_list.csv - KHONG render
choice /C 123 /M "Chon"
if errorlevel 3 goto script
if errorlevel 2 goto approve
"%PY%" -m lib.anime_claude_packet understanding --paste %PKT%
goto done
:approve
"%PY%" -m lib.anime_claude_packet approve %PKT%
goto done
:script
"%PY%" -m lib.anime_claude_packet script --paste %PKT%
:done
pause
