@echo off
REM Windows defaults to the Restricted execution policy, which blocks .ps1
REM files outright. -ExecutionPolicy Bypass applies to this one process only and
REM changes nothing machine-wide - it is what makes this wrapper double-clickable.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0stop-all.ps1" %*
REM Captured before anything else runs: pause itself succeeds and would erase it.
set "EXITCODE=%ERRORLEVEL%"
if not "%EXITCODE%"=="0" (
    echo.
    echo Digital Brain did not stop. The message above explains why.
    REM Double-clicked from Explorer, this window closes the instant the script
    REM ends - which is exactly the first-run case where the error matters most.
    pause
)
exit /b %EXITCODE%
