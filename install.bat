@echo off
rem Installs the packages DupeFinder needs from the wheels folder. Works offline.
setlocal
cd /d "%~dp0"
if not exist "wheels\" (
  echo This folder is DupeFinder's source code, which has nothing to install.
  echo Download the ready-to-run version instead. It includes its own Python:
  echo   GitHub, Actions tab, latest green run, Artifacts: DupeFinder-windows
  echo Unzip it, then double-click run.bat.
  goto :fail
)
rem The wheels folder holds packages for 64-bit Python 3.12, 3.13 and 3.14.
set "CHECK=import sys, struct; ok = (3, 12) <= sys.version_info[:2] <= (3, 14) and struct.calcsize('P') == 8 and getattr(sys, '_is_gil_enabled', lambda: True)(); sys.exit(0 if ok else 1)"
set "PY="
for %%V in (3.13 3.12 3.14) do (
  if not defined PY (
    py -%%V -c "%CHECK%" >nul 2>nul && set "PY=py -%%V"
  )
)
if not defined PY (
  python -c "%CHECK%" >nul 2>nul && set "PY=python"
)
if not defined PY (
  echo DupeFinder needs 64-bit Python 3.12, 3.13 or 3.14. Install one from python.org,
  echo then run install.bat again.
  goto :fail
)
if not exist ".venv\Scripts\python.exe" (
  echo Creating a private Python environment in .venv with %PY% ...
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
