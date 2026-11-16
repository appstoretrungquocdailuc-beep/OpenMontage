@echo off
chcp 65001 >nul
cd /d "%~dp0"
REM V9.8-SUB2S GUI (file V9_4_SUB2S.py). Mac dinh V9.8: prealloc nen + race learn + worker tu hoc.
REM Quay ve hanh vi V9.7.1 tung phan: bo REM o dong tuong ung ben duoi.
REM set V98_PREALLOC_ASYNC=0
REM set V98_RACE_MODE=fixed
REM set V98_WORKERS_LEARN=0
REM Thu nghiem (chi bat khi BENCH_V98_480.bat buoc 1 chung minh co loi tren may nay):
REM set V98_WRITE_MODE=pwrite
REM Ban V9.7.1 nguyen ven: CHEP backup\V9_7_1_SUB2S_truoc_v98.py ra thu muc nay roi chay file do
REM (ffmpeg\, aria2c.exe, config duoc tim theo thu muc chua file .py)
python V9_4_SUB2S.py
if errorlevel 1 pause
