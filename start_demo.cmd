@echo off
cd /d "%~dp0"
where py >nul 2>nul
if errorlevel 1 (
  python -B app.py --demo
) else (
  py -3 -B app.py --demo
)
if errorlevel 1 pause
