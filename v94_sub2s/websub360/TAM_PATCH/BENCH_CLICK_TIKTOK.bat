@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ============================================================
echo  BENCH CLICK - do tre "nut Post sang -> click"
echo  Bam that vao TRANG GIA 127.0.0.1 trong TAB MOI cua profile.
echo  KHONG mo TikTok, KHONG dang video nao, KHONG ghi nhat ky dang.
echo  Tat WS360/TAM dang dung profile nay truoc khi chay.
echo  Giu cua so profile giong luc dang that (vd thu nho / bi che) de do dung.
echo ============================================================
set /p PID=Profile ID Genlogin: 
set TAM_HIGH_PRIORITY=0
echo.
echo [1/2] driver (cach cu, nhu 5.3.0) va ws (cach moi) xen ke A-B-B-A, 20 vong x 2 = 40 luot (~3 phut)
python tam_INSTANT_POST.py --bench-click --profile %PID% --repeat 20 --click-paths driver,ws --window asis --yes
echo.
set /p COV=[2/2] Do them truong hop nut sang nhung con bi lop phu che 600 ms? 10 vong, ~2 phut. Go Y de chay: 
if /i "%COV%"=="Y" python tam_INSTANT_POST.py --bench-click --profile %PID% --repeat 10 --click-paths driver,ws --coverhold-ms 600 --window asis --yes
echo.
echo Gui cac file tam_bench_click_*.json va .md (canh file TAM) de phan tich.
pause
