@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
echo Do thoi gian LAY LINK cua v10fast (5 lan, nghi 2 s) + thu 1 KB cua link + so voi cach cu (yt-dlp tao moi moi video).
set /p U=Dan 1 URL YouTube roi Enter: 
python shorwweb_v10fast.py --bench-link %U%
pause
