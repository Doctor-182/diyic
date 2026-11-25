@echo off
chcp 65001 >nul
setlocal EnableExtensions
cd /d "%~dp0.."
call "scripts\_env.bat"
if errorlevel 1 ( pause & exit /b 1 )
echo 评测中（设备闭环 + A/B 两条链，约 90 秒）…
%PY% -m vrdub.bench --dev --lat 4
echo.
echo 报告：%LOCALAPPDATA%\VRDub\out\bench.md
pause
endlocal
