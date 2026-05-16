@echo off
:: FolderSync v4 — Windows runner
:: Shows a full pre-flight preview before syncing.
:: Press Y to confirm, N to abort.
::
:: To run without confirmation (e.g. Task Scheduler):
::   python syncv4.py --yes --config config.json

cd /d "%~dp0"

python syncv5format.py --config config.json

if %ERRORLEVEL% NEQ 0 (
    echo.
    echo [ERROR] Sync completed with errors. Check the pair log file(s) for details.
    pause
) else (
    echo.
    echo [OK] Done.
    pause
)
