@echo off
chcp 65001 >nul
setlocal EnableExtensions
cd /d "%~dp0.."

git remote get-url origin >nul 2>nul
if errorlevel 1 (
  echo [x] 还没配远端。先双击 scripts\首次上传到GitHub.bat 一次。
  pause & exit /b 1
)

echo [i] 本次要推的提交：
git log --oneline origin/main..HEAD 2>nul
echo.

git push origin main
if errorlevel 1 (
  echo.
  echo [x] 没推上去（见上面 git 的输出）。网络问题挂代理重试；权限问题检查仓库归属。
  pause & exit /b 1
)
echo [ok] 已推到 https://github.com/Doctor-182/diyic
pause
endlocal
