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
"%PY%" -c "import PIL" 2>nul
if errorlevel 1 (
  echo Cai thu vien Pillow - mot lan, can mang...
  "%PY%" -m pip install pillow
  if errorlevel 1 (
    echo [LOI] Khong cai duoc Pillow.
    pause
    exit /b 1
  )
)
set "RUNARG="
if not "%~1"=="" set RUNARG=--run "%~1"
echo ==== Xuat goi du lieu cho Claude Pro - KHONG goi API, KHONG chay lai Whisper ====
echo 1 = Tong quan ca tap - de Claude goi y MOT tinh huong hoan chinh 60-90 giay
echo 2 = Chi tiet MOT doan - nhap thoi gian bat dau va ket thuc
echo 3 = Bo sung khung hinh day cho cac khoang Claude yeu cau - goi moi nhat
choice /C 123 /M "Chon"
if errorlevel 3 goto more
if errorlevel 2 goto detail
"%PY%" -m lib.anime_claude_packet export --mode overview %RUNARG%
goto done
:detail
set /p START=Bat dau, vi du 2:10 : 
set /p END=Ket thuc, vi du 3:30 : 
"%PY%" -m lib.anime_claude_packet export --mode detail --start "%START%" --end "%END%" %RUNARG%
goto done
:more
set /p RANGES=Cac khoang, vi du 2:41-2:44, 3:02.5-3:04 : 
"%PY%" -m lib.anime_claude_packet more --ranges "%RANGES%"
:done
echo.
echo Goi nam trong: anime_trial\ket_qua\^<lan chay^>\claude_pro\
pause
