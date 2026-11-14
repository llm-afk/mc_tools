@echo off
setlocal
powershell.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "%~dp0drivers\setup_devices.ps1" -Mode Install
if errorlevel 1 (
  echo Driver preparation was not confirmed. See the workstation log or %%LOCALAPPDATA%%\MirrorMe\FactoryStation\driver-setup.
  pause
  exit /b 1
)
echo Connected device drivers have been checked. No print job was submitted.
pause
