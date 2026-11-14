@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ============================================================
echo  BENCH NET - upload TikTok THAT, DRY-RUN: KHONG bam Post
echo  A/B: base (nhu cu) vs pre (mo san ket noi upload khi phat hien video)
echo  Ghi moc mang CDP (khong ghi cookie/header/body). Xuat CSV + CSV tom tat.
echo  Tat WS360/TAM dang dung profile nay truoc khi chay.
echo ============================================================
set /p PID=Profile ID Genlogin: 
set /p VID=File video mp4 360p (keo tha vao day): 
set TAM_HIGH_PRIORITY=0
echo 10 vong x 2 = 20 luot (+1 luot hoc may chu upload neu chua co), moi luot ranh 30 s -> khoang 15-20 phut
python tam_INSTANT_POST.py --bench-net --profile %PID% --files %VID% --repeat 10 --net-idle-s 30 --net-lead-s 1.5 --yes
echo.
echo Gui cac file tam_bench_net_*.csv, *_summary.csv, *.md de phan tich.
pause
