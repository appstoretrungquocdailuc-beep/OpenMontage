@echo off
chcp 65001 >nul
cd /d "%~dp0"
title WS360 BROWSE ONLY 24/7 + TU DANG TIKTOK (khong giao dien, tu chay lai neu thoat)
if not exist config.json copy config.example.json config.json >nul
set TAM_HIGH_PRIORITY=0
echo CHU Y: ban nay DANG THAT len TikTok. Muon chay thu (khong bam Post): them --dry-run-post vao dong python ben duoi.
echo Khong mo cung luc giao dien WS360 / tool khac dung CUNG profile Genlogin.
:loop
echo [%date% %time%] Khoi dong WS360 BROWSE ONLY + TikTok ...
python YOUTUBE_WEBSUB_INSTANT_360P.py --headless --mode BROWSE_ONLY --integrated %*
echo [%date% %time%] WS360 da thoat (ma %errorlevel%) - chay lai sau 5 giay. Dong cua so nay de dung han.
timeout /t 5 /nobreak >nul
goto loop
