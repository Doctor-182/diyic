@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
set "PY=C:\Users\Lenovo\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe"
if not exist "%PY%" set "PY=python"
if exist "%~dp0.pylibs" set "PYTHONPATH=%~dp0.pylibs"
if "%PYTHONPATH%"=="" set "PYTHONPATH=D:\工作区2\_probe\pylibs"
set "PYTHONIOENCODING=utf-8"
echo 评测中（设备闭环 + A/B 两条链，约 90 秒）…
"%PY%" -m vrdub.bench --dev --lat 4
echo.
echo 报告：out\bench.md
pause
endlocal
