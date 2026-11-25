@echo off
chcp 65001 >nul
setlocal EnableExtensions
cd /d "%~dp0.."
call "scripts\_env.bat"
if errorlevel 1 pause & exit /b 1
%PY% "tools\lat_report.py" %*
pause
endlocal
