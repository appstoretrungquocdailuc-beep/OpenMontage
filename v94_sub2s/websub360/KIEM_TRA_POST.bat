@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist logs\rejected_posts (echo Chua co POST nao bi tu choi. & pause & exit /b)
for /f "delims=" %%F in ('dir /b /o-d logs\rejected_posts\*.json') do (set LAST=%%F& goto :run)
:run
echo Kiem tra: %LAST%
python YOUTUBE_WEBSUB_INSTANT_360P.py --verify-post "logs\rejected_posts\%LAST%"
pause
