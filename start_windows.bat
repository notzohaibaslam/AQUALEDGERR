@echo off
setlocal
cd /d "%~dp0"
where python >nul 2>nul
if errorlevel 1 (
 echo Python 3.11 or newer is required. Install Python and select "Add Python to PATH".
 pause
 exit /b 1
)
if not exist ".venv\Scripts\python.exe" (
 echo Creating local Python environment...
 python -m venv .venv
 if errorlevel 1 (echo Failed to create Python environment.&pause&exit /b 1)
)
echo Installing or updating AquaLedger dependencies (including Windows timezone data)...
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (echo Dependency installation failed. Check your internet connection.&pause&exit /b 1)
echo.
echo Checking platform administrator account...
".venv\Scripts\python.exe" -c "import main,sys; c=main.db(); ok=c.execute('SELECT 1 FROM platform_admins LIMIT 1').fetchone(); c.close(); sys.exit(0 if ok else 2)"
if errorlevel 1 (
 echo.
 echo First-time installation: set up your MASTER platform administrator.
 echo This is not a plant account and customers cannot create it from the website.
 ".venv\Scripts\python.exe" create_platform_admin.py
 if errorlevel 1 (echo Admin setup canceled.&pause&exit /b 1)
)
echo.
echo Starting AquaLedger Pro v5.0 at http://127.0.0.1:8000
echo Your browser opens automatically only after the server responds.
echo Keep this window open while using the application. Press Ctrl+C to stop.
echo.
".venv\Scripts\python.exe" run_local.py
if errorlevel 1 (echo AquaLedger stopped with an error. Read the error message above.)
pause
