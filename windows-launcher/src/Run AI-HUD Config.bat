@echo off
REM AI-HUD Config Launcher (Windows entry point).
REM
REM Double-clicking this .bat starts the PowerShell launcher. We have to
REM go through a .bat because:
REM   1. Windows Explorer won't run .ps1 by double-click (security policy).
REM   2. We can't change the user's PowerShell execution policy globally.
REM   3. `-ExecutionPolicy Bypass` on the command line is per-invocation
REM      only and doesn't touch the system setting.

setlocal

REM Resolve our own folder; %~dp0 ends with a trailing backslash.
set SCRIPT_DIR=%~dp0

REM Hide the console window (windowstyle Hidden) so the user just sees
REM native MessageBox dialogs plus the browser opening, not a cmd window.
powershell.exe ^
  -ExecutionPolicy Bypass ^
  -NoProfile ^
  -WindowStyle Hidden ^
  -File "%SCRIPT_DIR%launcher.ps1"

REM No pause -- the launcher script itself shows a dialog on error,
REM so we don't need cmd to stay open. The script exits with 0 on
REM success, non-zero on user-visible failure.
endlocal
