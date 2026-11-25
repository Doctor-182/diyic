@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0.."
echo.
echo  ============================================
echo   把本机提交推到 GitHub（断线自动重试）
echo  ============================================
echo.
echo  这是可重复执行的。网络时通时断也能用：
echo   脚本会每 12 秒重试一次，最多 60 次（约 12 分钟）。
echo   一旦某次连通，立刻推完就退出。
echo.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0push.ps1" -Attempts 60 -GapSec 12 -Force
echo.
if errorlevel 1 (
  echo  [x] 12 分钟内一次都没连上。
  echo      1^) 先跑一次 检查GitHub连接.bat 看看是 SSH 还是 HTTPS 断
  echo      2^) 网页能开就说明是 git 通道被拦，改用手机热点再跑本脚本
) else (
  echo  [v] 推上去了。打开 https://github.com/Doctor-182/diyic 看一眼。
)
echo.
pause
