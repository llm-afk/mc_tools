@echo off
setlocal
fltmc >nul 2>nul
if errorlevel 1 (
  echo Run this file as administrator to install USB drivers.
  pause
  exit /b 1
)
pnputil.exe /add-driver "%~dp0drivers\*.inf" /subdirs /install
if errorlevel 1 (
  echo Windows reported a driver installation error.
  pause
  exit /b 1
)
echo Driver processing finished. Reconnect USB devices and open the application.
pause
