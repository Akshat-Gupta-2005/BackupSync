@echo off
setlocal EnableDelayedExpansion

:: ============================================================
::  BIDIRECTIONAL FOLDER SYNC  v5
::
::  Rules:
::    CREATE  - file only in A -> copy to B  (and vice versa)
::    UPDATE  - file in both   -> newer timestamp wins
::    DELETE  - file deleted from one side since last run
::                             -> deleted from the other side too
::
::  How it works (TWO-PASS design):
::    Pass 1 (READ ONLY) - walk all files, compare timestamps,
::            build two decision lists before touching anything:
::              copy_a_to_b.tmp  - files to copy from A into B
::              copy_b_to_a.tmp  - files to copy from B into A
::    Pass 2 (WRITE)     - execute the lists.
::
::    This prevents the overwrite bug where Phase 2 writes a file
::    into B, giving it a fresh timestamp, and Phase 3 then sees
::    that fresh timestamp and copies the old version back over A.
::
::  State tracking:
::    .sync_snapshot.txt lives hidden inside each folder.
::    Used only for deletion detection across runs.
::
::  Usage:
::    sync_folders.bat "D:\path with spaces\FolderA" "D:\path\FolderB"
::    or double-click and type paths when prompted.
:: ============================================================

:: ---- Get folder paths ----------------------------------------
set "FOLDER_A=%~1"
set "FOLDER_B=%~2"

if "!FOLDER_A!"=="" set /p "FOLDER_A=Enter path for Folder A: "
if "!FOLDER_B!"=="" set /p "FOLDER_B=Enter path for Folder B: "

set "FOLDER_A=!FOLDER_A:"=!"
set "FOLDER_B=!FOLDER_B:"=!"

if "!FOLDER_A:~-1!"=="\" set "FOLDER_A=!FOLDER_A:~0,-1!"
if "!FOLDER_B:~-1!"=="\" set "FOLDER_B=!FOLDER_B:~0,-1!"

if not exist "!FOLDER_A!\" (
    echo [ERROR] Folder A not found: !FOLDER_A!
    pause & exit /b 1
)
if not exist "!FOLDER_B!\" (
    echo [ERROR] Folder B not found: !FOLDER_B!
    pause & exit /b 1
)

set "SNAP_A=!FOLDER_A!\.sync_snapshot.txt"
set "SNAP_B=!FOLDER_B!\.sync_snapshot.txt"

:: Temp decision lists (written during pass 1, executed in pass 2)
set "LIST_ATOB=%TEMP%\sync_atob_%RANDOM%.tmp"
set "LIST_BTOA=%TEMP%\sync_btoa_%RANDOM%.tmp"
if exist "!LIST_ATOB!" del /f /q "!LIST_ATOB!"
if exist "!LIST_BTOA!" del /f /q "!LIST_BTOA!"

echo.
echo ============================================================
echo   BIDIRECTIONAL SYNC  v5
echo   A : !FOLDER_A!
echo   B : !FOLDER_B!
echo ============================================================
echo.

set "cnt_del=0"
set "cnt_atob=0"
set "cnt_btoa=0"

:: ============================================================
:: PHASE 1  -  DELETION DETECTION
::
:: Read each folder's snapshot. Any file listed there but now
:: missing from disk was deleted -> delete from the other side.
:: ============================================================

echo [Phase 1] Checking for deletions...

if exist "!SNAP_A!" (
    for /f "usebackq tokens=* delims=" %%L in ("!SNAP_A!") do (
        set "rel=%%L"
        if /i not "!rel!"==".sync_snapshot.txt" (
            if not exist "!FOLDER_A!\!rel!" (
                if exist "!FOLDER_B!\!rel!" (
                    echo   [DEL from B] !rel!
                    del /f /q "!FOLDER_B!\!rel!" 2>nul
                    set /a cnt_del+=1
                )
            )
        )
    )
) else (
    echo   [A] No snapshot yet - deletion detection skipped ^(first run^)
)

if exist "!SNAP_B!" (
    for /f "usebackq tokens=* delims=" %%L in ("!SNAP_B!") do (
        set "rel=%%L"
        if /i not "!rel!"==".sync_snapshot.txt" (
            if not exist "!FOLDER_B!\!rel!" (
                if exist "!FOLDER_A!\!rel!" (
                    echo   [DEL from A] !rel!
                    del /f /q "!FOLDER_A!\!rel!" 2>nul
                    set /a cnt_del+=1
                )
            )
        )
    )
) else (
    echo   [B] No snapshot yet - deletion detection skipped ^(first run^)
)

:: ============================================================
:: PHASE 2  -  PASS 1: BUILD DECISION LISTS (no file writes)
::
:: Walk every file in A:
::   - Missing in B        -> queue for A->B copy
::   - Exists in both      -> use robocopy /L /XN to check if A
::                            is newer. /L = list only (no write).
::                            /XN = skip if dest newer.
::                            If robocopy outputs the filename,
::                            A is newer -> queue for A->B copy.
::
:: Walk every file in B:
::   - Missing in A        -> queue for B->A copy
::   - Exists in both      -> robocopy /L /XN to check if B newer
::                            -> queue for B->A copy
::
:: Files that exist in both are only queued in ONE direction
:: (whichever side is newer). Same-timestamp files go in neither.
:: ============================================================

echo.
echo [Phase 2] Scanning files and building copy plan...

:: --- Scan A: queue files to copy A -> B ---
for /r "!FOLDER_A!" %%F in (*) do (
    set "absA=%%~fF"
    set "nameF=%%~nxF"
    set "dirF=%%~dpF"
    :: Remove trailing backslash from dir
    set "dirF=!dirF:~0,-1!"

    :: Get relative path using length-based slice
    call :get_rel "!FOLDER_A!" "!absA!" relF

    if /i not "!relF!"==".sync_snapshot.txt" (

        set "absB=!FOLDER_B!\!relF!"

        if not exist "!absB!" (
            :: File only in A -> always copy to B
            echo !relF!>>"!LIST_ATOB!"
        ) else (
            :: File in both -> check if A is newer using robocopy /L /XN
            :: Compute matching dir inside B
            call :get_rel "!FOLDER_A!" "!dirF!" subdirA
            if "!subdirA!"=="" (
                set "matchDirB=!FOLDER_B!"
            ) else (
                set "matchDirB=!FOLDER_B!\!subdirA!"
            )
            :: robocopy /L /XN: lists file only if source (A) is newer than dest (B)
            :: /NJS /NJH /NDL /NS /NC = suppress all header and summary noise
            set "robo_hit="
            for /f "tokens=*" %%R in ('robocopy "!dirF!" "!matchDirB!" "!nameF!" /L /XN /NJS /NJH /NDL /NS /NC 2^>nul') do (
                set "robo_line=%%R"
                :: robocopy outputs a line containing the filename when it would copy
                echo !robo_line! | findstr /i "!nameF!" >nul 2>&1
                if !errorlevel! EQU 0 set "robo_hit=1"
            )
            if defined robo_hit (
                echo !relF!>>"!LIST_ATOB!"
            )
        )
    )
)

:: --- Scan B: queue files to copy B -> A ---
for /r "!FOLDER_B!" %%F in (*) do (
    set "absB=%%~fF"
    set "nameF=%%~nxF"
    set "dirF=%%~dpF"
    set "dirF=!dirF:~0,-1!"

    call :get_rel "!FOLDER_B!" "!absB!" relF

    if /i not "!relF!"==".sync_snapshot.txt" (

        set "absA=!FOLDER_A!\!relF!"

        if not exist "!absA!" (
            :: File only in B -> always copy to A
            echo !relF!>>"!LIST_BTOA!"
        ) else (
            :: File in both -> check if B is newer
            call :get_rel "!FOLDER_B!" "!dirF!" subdirB
            if "!subdirB!"=="" (
                set "matchDirA=!FOLDER_A!"
            ) else (
                set "matchDirA=!FOLDER_A!\!subdirB!"
            )
            set "robo_hit="
            for /f "tokens=*" %%R in ('robocopy "!dirF!" "!matchDirA!" "!nameF!" /L /XN /NJS /NJH /NDL /NS /NC 2^>nul') do (
                set "robo_line=%%R"
                echo !robo_line! | findstr /i "!nameF!" >nul 2>&1
                if !errorlevel! EQU 0 set "robo_hit=1"
            )
            if defined robo_hit (
                echo !relF!>>"!LIST_BTOA!"
            )
        )
    )
)

echo   Scan complete.

:: ============================================================
:: PHASE 3  -  PASS 2: EXECUTE COPIES FROM DECISION LISTS
::
:: Now that all decisions are frozen, actually copy the files.
:: For each relative path in the list:
::   - Build absolute source and destination paths
::   - Create destination subdirectory if needed
::   - Copy using robocopy (handles any filename, spaces, etc.)
:: ============================================================

echo.
echo [Phase 3] Copying A --^> B ...

if exist "!LIST_ATOB!" (
    for /f "usebackq tokens=* delims=" %%L in ("!LIST_ATOB!") do (
        set "rel=%%L"
        set "absA=!FOLDER_A!\!rel!"
        set "absB=!FOLDER_B!\!rel!"

        :: Get just the filename and the destination directory
        for %%X in ("!absB!") do (
            set "dstDir=%%~dpX"
            set "fname=%%~nxX"
        )
        set "dstDir=!dstDir:~0,-1!"

        if not exist "!dstDir!\" mkdir "!dstDir!" 2>nul
        robocopy "!FOLDER_A!" "!dstDir!" "!fname!" /NP /NJH /NJS 2>nul
        echo   [A->B] !rel!
        set /a cnt_atob+=1
    )
) else (
    echo   Nothing to copy A->B.
)

echo.
echo [Phase 3] Copying B --^> A ...

if exist "!LIST_BTOA!" (
    for /f "usebackq tokens=* delims=" %%L in ("!LIST_BTOA!") do (
        set "rel=%%L"
        set "absB=!FOLDER_B!\!rel!"
        set "absA=!FOLDER_A!\!rel!"

        for %%X in ("!absA!") do (
            set "dstDir=%%~dpX"
            set "fname=%%~nxX"
        )
        set "dstDir=!dstDir:~0,-1!"

        if not exist "!dstDir!\" mkdir "!dstDir!" 2>nul
        robocopy "!FOLDER_B!" "!dstDir!" "!fname!" /NP /NJH /NJS 2>nul
        echo   [B->A] !rel!
        set /a cnt_btoa+=1
    )
) else (
    echo   Nothing to copy B->A.
)

:: ============================================================
:: PHASE 4  -  SAVE SNAPSHOTS
:: ============================================================

echo.
echo [Phase 4] Saving state snapshots...

if exist "!SNAP_A!" del /f /q "!SNAP_A!"
for /r "!FOLDER_A!" %%F in (*) do (
    set "absF=%%~fF"
    call :get_rel "!FOLDER_A!" "!absF!" rel
    if /i not "!rel!"==".sync_snapshot.txt" (
        echo !rel!>>"!SNAP_A!"
    )
)
attrib +h "!SNAP_A!" >nul 2>&1

if exist "!SNAP_B!" del /f /q "!SNAP_B!"
for /r "!FOLDER_B!" %%F in (*) do (
    set "absF=%%~fF"
    call :get_rel "!FOLDER_B!" "!absF!" rel
    if /i not "!rel!"==".sync_snapshot.txt" (
        echo !rel!>>"!SNAP_B!"
    )
)
attrib +h "!SNAP_B!" >nul 2>&1

:: Cleanup temp lists
if exist "!LIST_ATOB!" del /f /q "!LIST_ATOB!"
if exist "!LIST_BTOA!" del /f /q "!LIST_BTOA!"

echo   Done.

:: ---- Summary -------------------------------------------------
echo.
echo ============================================================
echo   SYNC COMPLETE
echo   Deletions    : !cnt_del!
echo   A --^> B      : !cnt_atob!
echo   B --^> A      : !cnt_btoa!
echo ============================================================
echo.
pause
endlocal
exit /b 0


:: ============================================================
:: SUBROUTINE :get_rel  "BaseFolder"  "AbsPath"  OutputVar
::
:: Extracts the relative portion of AbsPath by measuring the
:: length of BaseFolder and slicing with :~N syntax.
:: Works with spaces and any special characters in the path.
:: ============================================================
:get_rel
set "_base=%~1"
set "_abs=%~2"
set "_out=%3"

set "_len=0"
set "_tmp=!_base!"
:_count
if "!_tmp!"=="" goto _done
set "_tmp=!_tmp:~1!"
set /a _len+=1
goto _count
:_done
set /a _len+=1

call set "_result=%%_abs:~!_len!%%"
set "!_out!=!_result!"
exit /b
