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
set TAM_HIGH_PRIORITY=0
echo.
echo [1/2] So sanh event / sparse / lite tren trang vua nap (20 vong x 3 = 60 luot, khoang 10-15 phut)
python tam_INSTANT_POST.py --bench-watch --profile %PID% --files %VID% --repeat 20 --modes ABC --page-states fresh --yes
echo.
set /p IDLE=[2/2] Do them: trang mo san 60 s roi moi gan file (giong luc cho video that)? 10 vong, ~15 phut. Go Y de chay: 
if /i "%IDLE%"=="Y" python tam_INSTANT_POST.py --bench-watch --profile %PID% --files %VID% --repeat 10 --modes A --page-states fresh,idle --idle-s 60 --yes
echo.
echo Gui cac file tam_bench_watch_*.json (canh file TAM) de phan tich.
pause
