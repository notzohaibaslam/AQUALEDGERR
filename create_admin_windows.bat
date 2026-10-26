@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
 echo Install dependencies first by running start_windows.bat, then return here.
 pause
 exit /b 1
)
".venv\Scripts\python.exe" create_platform_admin.py
if errorlevel 1 (echo Setup did not finish successfully.)
pause
