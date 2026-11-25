@echo off
chcp 65001 >nul
setlocal EnableExtensions
cd /d "%~dp0.."
call "scripts\_env.bat"
if errorlevel 1 ( pause & exit /b 1 )
echo 自检中（约 40 秒：真的去调一次识别/翻译/合成）…
%PY% -m vrdub.app --selftest
echo.
echo 结果：%LOCALAPPDATA%\VRDub\out\selftest.md
pause
endlocal
