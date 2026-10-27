@echo off
rem Runs DupeFinder. Without arguments it asks which folders or drives to scan.
rem Examples:  run.bat C:\ D:\      run.bat D:\Photos --types image
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Run install.bat first.
  pause
  exit /b 1
)
if "%~1"=="" goto :ask
".venv\Scripts\python.exe" dupefinder.py %*
exit /b %errorlevel%
:ask
set "ROOTS="
set /p "ROOTS=Folders or drives to scan (for example C:\ D:\): "
if not defined ROOTS exit /b 1
".venv\Scripts\python.exe" dupefinder.py %ROOTS%
pause
