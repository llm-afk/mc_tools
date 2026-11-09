@echo off
setlocal
title Factory Test Environment Setup

fltmc >nul 2>nul
if errorlevel 1 (
    echo Requesting administrator permission for driver installation...
    powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)

echo ============================================================
echo   Factory Test Environment Setup
echo ============================================================
echo.

set "FACTORY_DIR=%~dp0"
for %%I in ("%FACTORY_DIR%..\..") do set "PROJECT_ROOT=%%~fI"
set "DRIVER_DIR=%FACTORY_DIR%drivers"

echo [1/4] Installing device drivers...
echo       CANable WinUSB  VID_1D50^&PID_606F
echo       WCH CH343       VID_1A86^&PID_55D3
echo       WCH CH340       VID_1A86^&PID_7523
pnputil.exe /add-driver "%DRIVER_DIR%\*.inf" /subdirs /install
if errorlevel 1 echo NOTE: Windows reported that one or more driver packages were already present or unchanged.
echo Driver processing finished. Device availability will be checked by the factory-test application.
echo.

where py >nul 2>nul
if not errorlevel 1 (
    set "PYTHON_CMD=py -3"
) else (
    where python >nul 2>nul
    if errorlevel 1 goto :python_missing
    set "PYTHON_CMD=python"
)

%PYTHON_CMD% -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>nul
if errorlevel 1 goto :python_version

echo [2/4] Python detected:
%PYTHON_CMD% --version
echo.

echo [3/4] Checking pip...
%PYTHON_CMD% -m pip --version >nul 2>nul
if errorlevel 1 (
    echo pip not found. Installing pip...
    %PYTHON_CMD% -m ensurepip --upgrade
    if errorlevel 1 goto :failed
)

echo.
echo [4/4] Installing factory-test Python dependencies...
%PYTHON_CMD% -m pip install -e "%PROJECT_ROOT%[console,power,dynamometer,report,upload]"
if errorlevel 1 goto :failed

echo.
echo ============================================================
echo.
echo       [SUCCESS] ALL DEPENDENCIES INSTALLED SUCCESSFULLY
echo.
echo   Drivers and Python packages have been processed.
echo   You can now double-click the factory-test .pyw launcher.
echo.
echo ============================================================
echo.
pause
exit /b 0

:python_missing
echo [FAILED] Python was not found.
echo Install Python 3.11 or newer and enable Add Python to PATH.
goto :end_failed

:python_version
echo [FAILED] Python is older than 3.11 or could not be started.
echo Install Python 3.11 or newer.
goto :end_failed

:failed
echo.
echo [FAILED] Dependency installation did not complete.
echo Check the messages above and verify the network connection.

:end_failed
echo.
pause
exit /b 1
