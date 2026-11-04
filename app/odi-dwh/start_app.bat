@echo off
setlocal

cd /d "%~dp0"
title DWH Studio - Start

set "PYTHON_EXE=%CD%\.venv\Scripts\python.exe"
if not exist "%PYTHON_EXE%" (
    set "PYTHON_EXE=python"
)

echo.
echo  ==========================================
echo     DWH STUDIO - Start
echo  ==========================================
echo.
echo Starting local Flask process with one listener policy...
echo.

"%PYTHON_EXE%" -X utf8 local_app_control.py start --open-browser
set "EXIT_CODE=%ERRORLEVEL%"

echo.
if "%EXIT_CODE%"=="0" (
    echo DWH Studio start command finished.
) else (
    echo DWH Studio start command failed. Check logs\local_app_stderr.log.
)
echo.
exit /b %EXIT_CODE%
