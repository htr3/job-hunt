@echo off
REM Job Hunt AI Agent - Windows one-click launcher
REM Usage:  double-click, or run:  run_job_hunter.bat --platforms remoteok --no-apply

setlocal
cd /d "%~dp0"

if exist ".venv\Scripts\activate.bat" (
    call ".venv\Scripts\activate.bat"
) else if exist "venv\Scripts\activate.bat" (
    call "venv\Scripts\activate.bat"
)

where python >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Python was not found on PATH.
    echo Install Python 3.11+ from https://www.python.org/downloads/ and re-run.
    pause
    exit /b 1
)

if not exist "config.yaml" (
    echo config.yaml not found - launching first-run setup wizard...
    python job_hunter.py --setup
    if errorlevel 1 exit /b %errorlevel%
)

python job_hunter.py %*
set EXITCODE=%errorlevel%

if "%~1"=="" (
    echo.
    pause
)

exit /b %EXITCODE%
