@echo off
rem Starts the scanner detached (survives closing this window). Logs -> logs\scanner.log
cd /d "%~dp0"
if exist data\scanner.pid (
  echo A scanner.pid exists. If the scanner is not running, delete data\scanner.pid and retry.
  echo Use stop_scanner.bat to stop a running instance.
  pause
  exit /b 1
)
start "momentum-ignition-scanner" /min cmd /c "python -m scanner run >> logs\scanner.stdout.log 2>&1"
echo Scanner started (minimized window). Tail logs\scanner.log to follow it. Stop with stop_scanner.bat.
timeout /t 3 >nul
