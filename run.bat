@echo off
chcp 65001 >nul
setlocal EnableExtensions
cd /d "%~dp0"

rem ---- 解释器：优先本机已验证的那个，找不到就用 PATH 里的 python ----
set "PY=C:\Users\Lenovo\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe"
if not exist "%PY%" set "PY=python"

rem ---- 依赖库：优先项目内 .pylibs（想搬走就把 pylibs 目录复制成 .pylibs）----
set "PYTHONPATH="
if exist "%~dp0.pylibs" set "PYTHONPATH=%~dp0.pylibs"
if "%PYTHONPATH%"=="" set "PYTHONPATH=D:\工作区2\_probe\pylibs"
set "PYTHONIOENCODING=utf-8"
set "PYTHONUNBUFFERED=1"

echo ============================================
echo   VRChat 实时同传 · 控制台 http://127.0.0.1:8899/
echo   关掉这个窗口 = 停止。Ctrl+C 也一样。
echo   后端崩了会自动重启（最多连续 20 次），控制台会自动重连。
echo ============================================

rem 3 秒后再开浏览器：让后端先起来，页面直接连上（否则会先闪一下离线预览）
rem 被守护脚本（tools\keepalive.ps1）拉起时传 nobrowser，免得每 5 分钟弹一个标签页
if /i "%~1"=="nobrowser" goto skipbrowser
start "" /b cmd /c "timeout /t 3 >nul & start "" http://127.0.0.1:8899/"
:skipbrowser

set /a TRY=0
:run
set /a TRY+=1
"%PY%" -m vrdub.app
set "RC=%ERRORLEVEL%"
if "%RC%"=="0" goto done

rem 非 0 退出：崩了。等 2 秒自己拉起来，别让用户面对白屏。
echo.
echo [后端退出 rc=%RC%] 2 秒后自动重启（第 %TRY% 次）…
if %TRY% GEQ 20 goto giveup
timeout /t 2 >nul
goto run

:giveup
echo.
echo [连续崩了 20 次，停下来] 看 out\srv.err.log 和 out\srv.out.log 的最后 30 行；
echo 先跑 检查环境.bat（结果写 out\selftest.md）。
pause
goto done

:done
endlocal
