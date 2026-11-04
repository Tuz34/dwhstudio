@echo off
setlocal

cd /d "%~dp0"
title DWH Studio - Stop

set "PYTHON_EXE=%CD%\.venv\Scripts\python.exe"
if not exist "%PYTHON_EXE%" (
    set "PYTHON_EXE=python"
)

echo.
echo  ==========================================
echo     DWH STUDIO - Stop
echo  ==========================================
echo.
echo Stopping local Flask listener on the configured port...
echo.

"%PYTHON_EXE%" -X utf8 local_app_control.py stop
set "EXIT_CODE=%ERRORLEVEL%"

echo.
if "%EXIT_CODE%"=="0" (
    echo DWH Studio stop command finished.
) else (
    echo DWH Studio stop command could not stop every listener.
)
echo.
exit /b %EXIT_CODE%
