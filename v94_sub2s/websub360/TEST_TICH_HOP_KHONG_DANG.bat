@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist config.json copy config.example.json config.json >nul
set TAM_HIGH_PRIORITY=0
echo CHAY THU TICH HOP: mo profile, tai video, gan file, CHO NUT POST SANG roi DUNG - KHONG BAM POST
python YOUTUBE_WEBSUB_INSTANT_360P.py --integrated --dry-run-post
pause
