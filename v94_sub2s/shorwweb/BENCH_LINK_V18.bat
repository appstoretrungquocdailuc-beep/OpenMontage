@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
echo Do thoi gian LAY LINK cua v18 (5 lan/URL, nghi 2 s) + thu 1 KB cua link (biet ngay link tai duoc hay bi 403) + so voi cach v17.
set /p U=Dan 1 URL YouTube roi Enter: 
python shorwweb_v18_fast360.py --bench-link %U%
pause
