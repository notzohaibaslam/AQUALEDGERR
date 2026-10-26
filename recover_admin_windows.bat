@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (echo Start the software once to install Python dependencies.&pause&exit /b 1)
".venv\Scripts\python.exe" recover_platform_admin.py
pause
