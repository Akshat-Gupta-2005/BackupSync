@echo off
:: FolderSync — Windows runner
:: Double-click to run, or add to Task Scheduler

cd /d "%~dp0"

echo ================================
echo   FolderSync - Two-Way Sync
echo ================================

python syncv2.py --config config.json

if %ERRORLEVEL% NEQ 0 (
    echo.
    echo [ERROR] Sync completed with errors. Check sync.log for details.
) else (
    echo.
    echo [OK] Sync completed successfully.
)

pause
