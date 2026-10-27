@echo off
rem Installs the packages DupeFinder needs from the wheels folder. Works offline.
setlocal
cd /d "%~dp0"
set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY where python >nul 2>nul && set "PY=python"
if not defined PY (
  echo Python 3.12 or newer is needed. Install it from python.org, then run this again.
  goto :fail
)
%PY% -c "import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)"
if errorlevel 1 (
  echo DupeFinder needs Python 3.12 or newer.
  goto :fail
)
if not exist ".venv\Scripts\python.exe" (
  echo Creating a private Python environment in .venv ...
  %PY% -m venv .venv
  if errorlevel 1 goto :fail
)
echo Installing packages from the wheels folder (no internet needed) ...
".venv\Scripts\python.exe" -m pip install --no-index --find-links wheels --disable-pip-version-check -r requirements.txt
if errorlevel 1 goto :fail
".venv\Scripts\python.exe" dupefinder.py --version
echo.
echo Done. Start DupeFinder with run.bat
if not defined DUPEFINDER_NO_PAUSE pause
exit /b 0
:fail
echo Installation did not finish.
if not defined DUPEFINDER_NO_PAUSE pause
exit /b 1
