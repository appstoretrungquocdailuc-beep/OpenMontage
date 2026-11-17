@echo off
chcp 65001 >nul
cd /d "%~dp0"
REM BAN UNG VIEN V9.9 (V9_9_CANDIDATE.py). Ban chinh V9.8.1 (V9_4_SUB2S.py / RUN_V9_8_GUI.bat) KHONG bi sua.
REM Mac dinh ban ung vien = hanh vi V9.8.1 + tail-rescue + do chi tiet tung range. Format van 244 - 135 + audio JA 139.
REM --- Bat SMART (tu chon format nhanh nhat trong cac codec cho phep):
REM set V99_FORMAT_MODE=smart
REM --- Cho phep AV1 (itag 397) CHI SAU KHI da kiem file ra AV1 chay duoc trong TikTok / trinh xem cua ban:
REM set V99_ALLOW_VIDEO_CODECS=av01,vp9,avc1
REM --- Tat tung phan:
REM set V99_TAIL_RESCUE=0
REM set V99_NET_TRACE=0
REM set V99_FORMAT_LEARN=0
python V9_9_CANDIDATE.py
if errorlevel 1 pause
