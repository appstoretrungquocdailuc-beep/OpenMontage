@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ============================================================
echo  BENCH A/B/C - co che theo doi nut Post (TikTok THAT)
echo  DRY-RUN: KHONG bam Post, KHONG dang video nao.
echo  Tat tool WS360/TAM dang dung profile nay truoc khi chay.
echo ============================================================
set /p PID=Profile ID Genlogin: 
set /p VID=Duong dan file video (keo tha file vao day): 
set /p REP=So vong (Enter = 6): 
if "%REP%"=="" set REP=6
set TAM_HIGH_PRIORITY=0
python tam_INSTANT_POST.py --bench-watch --profile %PID% --files %VID% --repeat %REP% --yes
pause
