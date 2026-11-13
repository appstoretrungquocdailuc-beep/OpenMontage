@echo off
chcp 65001 >nul
cd /d "%~dp0"
title WS360 API + BROWSE
if not exist config.json copy config.example.json config.json >nul
set TAM_HIGH_PRIORITY=0
python YOUTUBE_WEBSUB_INSTANT_360P.py --mode API_BROWSE %*
pause
