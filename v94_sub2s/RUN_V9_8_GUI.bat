@echo off
chcp 65001 >nul
cd /d "%~dp0"
REM V9.8.1-SUB2S GUI (file V9_4_SUB2S.py). Mac dinh: prealloc nen + DUAL (video: 2N ket noi deu tai) + ETA tu luc co header.
REM Quay ve hanh vi V9.7.1 tung phan: bo REM o dong tuong ung ben duoi.
REM set V98_PREALLOC_ASYNC=0
REM set V98_RACE_MODE=fixed
REM     (hoac: set V98_RACE_MODE=learn  = ban V9.8 hom 16/11)
REM set V98_WORKERS_LEARN=0
REM set V98_ETA_FIRST_BYTE=0
REM TU SO SANH ARIA2C (toi da 16 ket noi/may chu; 403/429 khong thu lai):
REM set V98_FORCE_ARIA2=1
REM Thu nghiem (chi bat khi BENCH_V98_480.bat buoc 1 chung minh co loi tren may nay):
REM set V98_WRITE_MODE=pwrite
REM Ban V9.7.1 nguyen ven: CHEP backup\V9_7_1_SUB2S_truoc_v98.py ra thu muc nay roi chay file do
REM (ffmpeg\, aria2c.exe, config duoc tim theo thu muc chua file .py)
python V9_4_SUB2S.py
if errorlevel 1 pause
