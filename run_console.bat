@echo off
title Momentum Ignition Scanner
cd /d "%~dp0"
if not exist data\scanner.pid goto run
set /p OLDPID=<data\scanner.pid
tasklist /FI "PID eq %OLDPID%" 2>nul | find "%OLDPID%" >nul
if errorlevel 1 goto stale
echo.
echo  Scanner is already running in the background as PID %OLDPID%.
choice /C YN /M " Stop it and run here in this window instead"
if errorlevel 2 exit /b 0
taskkill /PID %OLDPID% /T /F >nul 2>&1
del data\scanner.pid >nul 2>&1
goto run
:stale
del data\scanner.pid >nul 2>&1
:run
echo.
echo  Momentum Ignition Scanner - press Ctrl+C to stop.
echo  Log: logs\scanner.log
echo.
python -m scanner run
echo.
echo  Scanner exited (code %errorlevel%).
pause
