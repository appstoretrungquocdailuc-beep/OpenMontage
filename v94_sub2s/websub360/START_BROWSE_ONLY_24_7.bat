@echo off
chcp 65001 >nul
cd /d "%~dp0"
title WS360 BROWSE ONLY 24/7 (khong giao dien, tu dung lai neu thoat)
if not exist config.json copy config.example.json config.json >nul
set TAM_HIGH_PRIORITY=0
:loop
echo [%date% %time%] Khoi dong WS360 BROWSE ONLY ...
python YOUTUBE_WEBSUB_INSTANT_360P.py --headless --mode BROWSE_ONLY %*
echo [%date% %time%] WS360 da thoat (ma %errorlevel%) - chay lai sau 5 giay. Dong cua so nay de dung han.
timeout /t 5 /nobreak >nul
goto loop
