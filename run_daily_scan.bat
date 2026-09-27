@echo off
setlocal
REM Full sync. Runs from whatever folder this file is in - move/rename the folder freely.
cd /d "%~dp0"
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
python master_sync_all.py
pause
endlocal
