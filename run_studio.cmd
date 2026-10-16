@echo off
rem LightDB Studio one-click launcher -- double-click this file.
rem
rem Why a .cmd wrapper: on some machines the PowerShell execution policy is
rem Restricted, so running the .ps1 directly fails with "running scripts is
rem disabled". Batch files are not affected; -ExecutionPolicy Bypass applies
rem to this single invocation only and does not change system settings.
rem
rem It will:
rem   1) start a single-process server on 127.0.0.1:8899 in the background
rem      (unless something is already listening there);
rem   2) start the GUI in the foreground. The GUI auto-connects using
rem      %USERPROFILE%\.lightdb_studio\connections.json and lists the tables.
setlocal
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0run_studio.ps1" %*
set EXITCODE=%ERRORLEVEL%
if not "%EXITCODE%"=="0" (
    echo.
    echo [LightDB] startup failed, exit code %EXITCODE%
    pause
)
endlocal & exit /b %EXITCODE%
