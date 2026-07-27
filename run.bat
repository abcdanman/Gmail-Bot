@echo off
cd /d "%~dp0"
set "PYTHON=%LOCALAPPDATA%\Python\bin\python.exe"
if not exist "%PYTHON%" set "PYTHON=python"
"%PYTHON%" -c "import mcp,googleapiclient" >nul 2>&1
if errorlevel 1 "%PYTHON%" -m pip install -r requirements.txt
"%PYTHON%" gmail_cleaner_mcp.py
if errorlevel 1 pause
