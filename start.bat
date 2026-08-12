@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"
title Local Color Service V0.6.9

echo ==================================================
echo  Starting Local Color Service V0.6.9
echo ==================================================

:: 1. Locate localcolor Python executable
set "PYTHON_EXE=C:\Users\Administrator\.conda\envs\localcolor\python.exe"

if not exist "!PYTHON_EXE!" (
    echo Searching for system Python or conda environment...
    where python >nul 2>&1
    if !ERRORLEVEL! equ 0 (
        set "PYTHON_EXE=python"
    ) else (
        echo [ERROR] Could not locate Python executable at !PYTHON_EXE!
        echo Please ensure Conda environment 'localcolor' is installed.
        pause
        exit /b 1
    )
)

echo Using Python: !PYTHON_EXE!

:: 2. Run Environment Doctor Check
echo.
echo Running Environment Doctor Check...
"!PYTHON_EXE!" scripts/doctor.py
if %ERRORLEVEL% NEQ 0 (
    echo.
    echo [ERROR] Environment doctor check failed!
    pause
    exit /b 1
)

:: 3. Start FastAPI Server
echo.
echo Starting FastAPI Uvicorn Server on http://127.0.0.1:8000 ...
echo Press Ctrl+C to stop the server.
echo.

"!PYTHON_EXE!" -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --log-level info

if %ERRORLEVEL% NEQ 0 (
    echo.
    echo [ERROR] Server exited with error code %ERRORLEVEL%.
)

pause
