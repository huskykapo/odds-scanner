@echo off
rem Start the arbitrage scanner on Windows. First run creates .venv and installs the dependencies.
rem Extra arguments are passed through, e.g.:  start.bat probe
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo Creating virtual environment in .venv ...
    py -3 -m venv .venv 2>nul || python -m venv .venv
    if errorlevel 1 (
        echo Python 3.11+ is required: https://www.python.org/downloads/  ^(tick "Add python.exe to PATH"^)
        pause
        exit /b 1
    )
    ".venv\Scripts\python.exe" -m pip install --upgrade pip
    ".venv\Scripts\python.exe" -m pip install -e .
)
".venv\Scripts\python.exe" -m odds_scanner %*
pause
