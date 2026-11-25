@echo off
chcp 65001 >nul
setlocal EnableExtensions
cd /d "%~dp0.."
call "scripts\_env.bat"
if errorlevel 1 ( pause & exit /b 1 )
echo 打源码包（发资料用；发软件请走 scripts\打包成exe.bat，稍后补）
%PY% "tools\pack.py" %*
echo.
echo 打完：dist\
pause
endlocal
