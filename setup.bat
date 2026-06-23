@echo off
TITLE MediaVault — First-Time Setup
echo ============================================
echo   MediaVault — First-Time Setup
echo ============================================
echo.

:: Check Python
python --version >nul 2>&1
IF ERRORLEVEL 1 (
    echo [ERROR] Python is not installed or not in PATH.
    echo         Download it from https://www.python.org/downloads/
    echo         Make sure to tick "Add Python to PATH" during install.
    pause
    exit /b 1
)

:: Create virtual environment
IF NOT EXIST "venv" (
    echo [1/3] Creating virtual environment...
    python -m venv venv
) ELSE (
    echo [1/3] Virtual environment already exists, skipping.
)

:: Activate and install dependencies
echo [2/3] Installing dependencies...
call venv\Scripts\activate.bat
pip install --upgrade pip --quiet
pip install -r requirements.txt --quiet

:: Run once to generate API key and print it
echo [3/3] Starting server to generate API key...
echo.
echo ============================================
echo   IMPORTANT: Note down your API Key below.
echo   You will paste it into the iOS Shortcut.
echo ============================================
echo.
python server.py
pause
