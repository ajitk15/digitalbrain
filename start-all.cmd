@echo off
REM Windows defaults to the Restricted execution policy, which blocks .ps1
REM files outright. -ExecutionPolicy Bypass applies to this one process only and
REM changes nothing machine-wide - it is what makes this wrapper double-clickable.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-all.ps1" %*
