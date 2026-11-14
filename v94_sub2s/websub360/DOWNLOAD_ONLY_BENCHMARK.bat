@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ==============================================================
echo  WS360 - DOWNLOAD ONLY BENCHMARK (dung thuat toan tai cua tool)
echo  Resolver Android Innertube (android_vr / android / web_embedded)
echo  -^> itag 18 -^> tai (HTTP cua tool nguoi/am + aria2c) -^> ffprobe
echo  Khong quet API/WebSub, khong ghi Excel, khong mo TAM, khong dang TikTok.
echo  Database rieng (database_DLBENCH.sqlite) - database.sqlite KHONG bi dung toi.
echo ==============================================================
where python >nul 2>nul || (echo [LOI] Chua cai Python 3.10+ ^(tick "Add to PATH"^). & pause & exit /b 1)
python -m pip install -q --disable-pip-version-check urllib3 yt-dlp
set FFP=
if exist "%~dp0ffprobe.exe" set FFP=1
if exist "%~dp0bin\ffprobe.exe" set FFP=1
if exist "%~dp0ffmpeg\bin\ffprobe.exe" set FFP=1
where ffprobe >nul 2>nul && set FFP=1
if not defined FFP (
  echo [LOI] Khong thay ffprobe.exe - chep ffprobe.exe vao thu muc nay ^(hoac ffmpeg\bin\^) roi chay lai.
  echo       Khong co ffprobe thi khong xac minh duoc video.
  pause
  exit /b 1
)
set A2=
if exist "%~dp0aria2c.exe" set A2=1
if exist "%~dp0bin\aria2c.exe" set A2=1
where aria2c >nul 2>nul && set A2=1
if not defined A2 echo [CHU Y] Khong thay aria2c.exe - chi do HTTP downloader cua tool ^(chep aria2c.exe vao day de so sanh^).
set VID=
set /p VID=Video ID hoac link YouTube (Enter = pk7HiB167VY): 
if "%VID%"=="" set VID=pk7HiB167VY
python YOUTUBE_WEBSUB_INSTANT_360P.py --download-bench %VID% --reps 5
echo.
echo Ket qua: DOWNLOAD_ONLY_BENCHMARK.md (resolver tung client + tung luot + median/P90) va .csv - gui 2 file nay.
pause
