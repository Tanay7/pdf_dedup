@echo off
rem Runs DupeFinder. Without arguments it opens a window to choose folders or drives.
rem Examples:  run.bat C:\ D:\      run.bat D:\Photos --types image
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Run install.bat first.
  pause
  exit /b 1
)
if "%~1"=="" goto :pick
".venv\Scripts\python.exe" dupefinder.py %*
exit /b %errorlevel%
:pick
".venv\Scripts\python.exe" dupefinder.py --pick
pause
