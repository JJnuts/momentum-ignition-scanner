@echo off
cd /d "%~dp0"
if not exist data\scanner.pid (
  echo No data\scanner.pid - scanner does not appear to be running.
  pause
  exit /b 0
)
set /p PID=<data\scanner.pid
echo Stopping scanner PID %PID% ...
taskkill /PID %PID% /T /F >nul 2>&1
del data\scanner.pid >nul 2>&1
echo Stopped.
timeout /t 2 >nul
