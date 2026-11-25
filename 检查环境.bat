@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
set "PY=C:\Users\Lenovo\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe"
if not exist "%PY%" set "PY=python"
if exist "%~dp0.pylibs" set "PYTHONPATH=%~dp0.pylibs"
if "%PYTHONPATH%"=="" set "PYTHONPATH=D:\工作区2\_probe\pylibs"
set "PYTHONIOENCODING=utf-8"
echo 自检中（约 40 秒：真的去调一次识别/翻译/合成）…
"%PY%" -m vrdub.app --selftest
echo.
echo 结果：out\selftest.md
pause
endlocal
