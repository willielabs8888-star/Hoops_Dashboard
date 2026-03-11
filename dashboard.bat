@echo off
REM ─────────────────────────────────────────────────
REM  FantasyBot ESPN Dashboard Launcher
REM  Double-click to open in your default browser
REM ─────────────────────────────────────────────────
cd /d "%~dp0"

if exist "venv\Scripts\python.exe" (
    set PYTHON=venv\Scripts\python.exe
) else (
    set PYTHON=python
)

echo.
echo  Starting FantasyBot ESPN Dashboard...
echo  (Your browser will open automatically)
echo.
%PYTHON% -m streamlit run app/dashboard/main.py
pause
