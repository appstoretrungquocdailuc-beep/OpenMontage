@echo off
chcp 65001 >nul
cd /d "%~dp0"
REM V9.4 SUB-2S GUI. Rollback: chay RUN_V9_3_GUI.bat (file V9_3_AUDIO.py giu nguyen).
REM Tat 1 toi uu V9.4 bang bien moi truong, vi du:  set V94_RANGE_ENGINE=v93
python V9_4_SUB2S.py
if errorlevel 1 pause
