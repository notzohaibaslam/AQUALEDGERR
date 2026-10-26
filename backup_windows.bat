@echo off
setlocal
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" backup_database.py
) else (
  python backup_database.py
)
if errorlevel 1 (echo Backup FAILED. Do not delete your existing records.)
echo.
echo Check the data\backups folder for the generated ZIP.
echo Copy the backup to a secure location outside this computer.
pause
