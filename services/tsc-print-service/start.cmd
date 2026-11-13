@echo off
setlocal
cd /d "%~dp0"
echo TSC Print Service - http://127.0.0.1:9876
echo Keep this window open for manual debugging.
if exist "%LOCALAPPDATA%\MirrorMe\FactoryStation\printer-service\config.json" (
    "%~dp0node.exe" "%~dp0index.js" --config "%LOCALAPPDATA%\MirrorMe\FactoryStation\printer-service\config.json"
) else (
    "%~dp0node.exe" "%~dp0index.js"
)
pause
