@echo off
:: FolderSync v3 — Windows runner
:: Supports any number of folders defined in config.json
:: Double-click to run, or add to Task Scheduler

cd /d "%~dp0"

echo ================================
echo   FolderSync v3 - Multi-Folder
echo ================================

python syncv3.py --config config.json

if %ERRORLEVEL% NEQ 0 (
    echo.
    echo [ERROR] Sync completed with errors. Check sync.log for details.
) else (
    echo.
    echo [OK] Sync completed successfully.
)

pause
