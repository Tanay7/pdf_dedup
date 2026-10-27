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
  echo This folder has no python folder, so it is the source code or an incomplete unzip.
  echo Download the ready-to-run version: GitHub, Actions tab, latest green run,
  echo Artifacts: DupeFinder-windows. Unzip it fully, then double-click run.bat.
  pause
  exit /b 1
)
if "%~1"=="" goto :pick
"%PY%" dupefinder.py %*
exit /b %errorlevel%
:pick
"%PY%" dupefinder.py --pick
pause
