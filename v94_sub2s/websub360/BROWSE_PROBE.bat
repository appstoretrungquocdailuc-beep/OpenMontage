@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo Goi Browse 1 lan cho cac kenh (Enter = 5 kenh dau cot K) va in tung dong + chu tuoi + ket luan.
echo Khong tai, khong ghi Excel/database that.
set /p CH=Channel ID (cach nhau dau cach, Enter = cot K): 
python YOUTUBE_WEBSUB_INSTANT_360P.py --browse-probe %CH%
pause
