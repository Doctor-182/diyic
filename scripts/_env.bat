@echo off
rem ---- 公共环境：找出 Python、拼好 PYTHONPATH。用 call 调用，不改调用者的 setlocal ----
rem 优先级：VRDUB_PYTHON 环境变量 > 便携运行时 .runtime\python.exe > py 启动器 > PATH 里的 python
set "PY="
for %%i in ("%~dp0..") do set "ROOT=%%~fi"

if defined VRDUB_PYTHON if exist "%VRDUB_PYTHON%" set "PY="%VRDUB_PYTHON%""
if defined PY goto have_py

if exist "%ROOT%\.runtime\python.exe" set "PY="%ROOT%\.runtime\python.exe""
if defined PY goto have_py

py -3.12 -c "import sys" >nul 2>nul
if not errorlevel 1 set "PY=py -3.12"
if defined PY goto have_py

py -3 -c "import sys" >nul 2>nul
if not errorlevel 1 set "PY=py -3"
if defined PY goto have_py

python -c "import sys" >nul 2>nul
if not errorlevel 1 set "PY=python"
if defined PY goto have_py

echo.
echo   [x] 没找到 Python。
echo       装一个 Python 3.12（勾 "Add to PATH"），或者把这个程序当作便携版使用
echo       （便携版自带 .runtime\python.exe，不需要系统装 Python）。
echo.
exit /b 1

:have_py
rem 依赖库跟着程序走：优先 .pylibs（便携/绿色版），其次就用系统里装好的包
if exist "%ROOT%\.pylibs" (
  set "PYTHONPATH=%ROOT%\.pylibs;%ROOT%"
) else (
  set "PYTHONPATH=%ROOT%"
)
set "PYTHONIOENCODING=utf-8"
set "PYTHONUNBUFFERED=1"
set "VRDUB_ROOT=%ROOT%"
exit /b 0
