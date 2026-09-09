@echo off
rem Starts the scanner as a HIDDEN background process (no window, cannot be closed by accident).
rem Logs -> logs\scanner.log. Stop with stop_scanner.bat, or take over from the Desktop launcher.
cd /d "%~dp0"
if exist data\scanner.pid (
  set /p OLDPID=<data\scanner.pid
  tasklist /FI "PID eq %OLDPID%" 2>nul | find "%OLDPID%" >nul
  if not errorlevel 1 (
    echo Scanner already running as PID %OLDPID%. Use stop_scanner.bat first.
    pause
    exit /b 1
  )
  del data\scanner.pid >nul 2>&1
)
powershell -NoProfile -Command "Start-Process -FilePath python -ArgumentList '-m','scanner','run' -WorkingDirectory '%~dp0' -WindowStyle Hidden -RedirectStandardOutput '%~dp0logs\scanner.stdout.log' -RedirectStandardError '%~dp0logs\scanner.stderr.log'"
echo Scanner started HIDDEN. Follow logs\scanner.log. Stop with stop_scanner.bat.
timeout /t 3 >nul
