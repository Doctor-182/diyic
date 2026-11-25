@echo off
chcp 65001 >nul
cd /d "%~dp0.."
call scripts\_env.bat
if errorlevel 1 goto :eof
echo.
echo   子密钥网关：主密钥留在这台机器，别人拿子密钥来连。
echo   配置：%LOCALAPPDATA%\VRDub\config\relay.json
echo   首次使用先跑：  python tools\relay.py --init
echo.
%PY% tools\relay.py %*
pause
