@echo off
title Stop Local Color Service

echo ==================================================
echo  Stopping Local Color Service
echo ==================================================

for /f "tokens=5" %%a in ('netstat -aon ^| findstr :8000 ^| findstr LISTENING') do (
    echo Terminating Process PID: %%a listening on port 8000...
    taskkill /F /PID %%a
)

echo Local Color Service stopped.
pause
