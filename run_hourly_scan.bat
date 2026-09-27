@echo off
setlocal
REM Quick refresh while the market is open: 1H scan only (daily is skipped if already done today).
cd /d "%~dp0"
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
python scan_market_structure_combined.py --timeframes 1h
pause
endlocal
