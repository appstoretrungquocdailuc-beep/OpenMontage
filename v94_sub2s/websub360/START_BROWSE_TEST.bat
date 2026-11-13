@echo off
chcp 65001 >nul
cd /d "%~dp0"
title WS360 BROWSE TEST (giao dien)
if not exist config.json copy config.example.json config.json >nul
python -m pip install -q --disable-pip-version-check -r requirements.txt
echo Chon "Nguon phat hien": API + BROWSE  hoac  BROWSE ONLY (khong API, khong ngrok) roi bam START.
echo KHUYEN NGHI khi thu: tick "Chay thu (KHONG bam Post)".
python YOUTUBE_WEBSUB_INSTANT_360P.py
pause
