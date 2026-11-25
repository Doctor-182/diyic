@echo off
chcp 65001 >nul
setlocal EnableExtensions
cd /d "%~dp0"

set "PY=C:\Users\Lenovo\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe"
if not exist "%PY%" set "PY=python"
if exist "%~dp0.pylibs" set "PYTHONPATH=%~dp0.pylibs"
if not defined PYTHONPATH if exist "D:\工作区2\_probe\pylibs" set "PYTHONPATH=D:\工作区2\_probe\pylibs"
set "PYTHONIOENCODING=utf-8"

echo 正在打包：dist\vrc-dub-src-日期.zip（代码）+ dist\vrc-dub-models-日期.zip（模型）
"%PY%" "%~dp0tools\pack.py" %*
echo.
echo 打完。把两个 zip 一起发给朋友：先解压代码包，再把模型包里的 models 覆盖进去。
pause
