@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist config.json copy config.example.json config.json >nul
echo ============================================================
echo  BENCH TAI 360P THAT - toi uu tai v2 (moi bien the bat THEM 1 nhom)
echo  legacy, warm_all (moc), v2_ssl, v2_redirect, v2_hedge, v2_full, v2_full_auto, warm_all_auto
echo  Moi luot lay link MOI va tai lai tu dau (khong dung file co san).
echo  Nen dung 3-4 video Shorts khac dung luong (vd 3 MB, 6 MB, 9 MB).
echo  Tat tool WS360 dang chay truoc khi do (de mang khong bi chia).
echo ============================================================
set /p IDS=Nhap 3-4 ID hoac link video (cach nhau dau cach): 
python YOUTUBE_WEBSUB_INSTANT_360P.py --bench-dl %IDS% --variants legacy,warm_all,v2_ssl,v2_redirect,v2_hedge,v2_full,v2_full_auto,warm_all_auto
echo.
echo Ket qua: BENCH_DL.md (bang), BENCH_DL_SUMMARY.md (tu ket luan bien the nao nen bat), BENCH_DL.csv (tung luot).
echo Gui 3 file nay de phan tich.
pause
