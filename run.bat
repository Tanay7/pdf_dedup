@echo off
rem Runs DupeFinder. Without arguments it opens a window to choose folders or drives.
rem Examples:  run.bat C:\ D:\      run.bat D:\Photos --types image
setlocal
cd /d "%~dp0"
rem The download includes its own Python in the python folder; nothing to install.
set "PY="
if exist "python\python.exe" set "PY=python\python.exe"
if not defined PY if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
if not defined PY (
  echo The python folder is missing. Unzip the whole DupeFinder download and run this again.
  pause
  exit /b 1
)
if "%~1"=="" goto :pick
"%PY%" dupefinder.py %*
exit /b %errorlevel%
:pick
"%PY%" dupefinder.py --pick
pause
