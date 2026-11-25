@echo off
chcp 65001 >nul
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0.."

git remote get-url origin >nul 2>nul
if errorlevel 1 (
  echo [x] 还没配远端。先双击 scripts\首次上传到GitHub.bat 一次。
  pause & exit /b 1
)

echo [1/3] 把改动记进版本库
git add -A
git diff --cached --quiet
if errorlevel 1 (
  for /f "tokens=1-3 delims=/ " %%a in ("%date%") do set "D=%%a-%%b-%%c"
  git -c user.name="VRDub" -c user.email="vrdub@local" commit -q -m "chore: 日常存档 %D%"
  echo     已提交
) else (
  echo     没有新改动
)

echo.
echo [2/3] 本次要推的提交：
git log --oneline origin/main..HEAD 2>nul
echo.

echo [3/3] 推送
git push origin main
if errorlevel 1 (
  echo.
  echo [x] 没推上去。看上面报错：
  echo     网络被掐 -^> 先开 sing-box（2080），再双击本脚本
  echo     认不出身份 -^> 双击 scripts\首次上传到GitHub.bat 重新授权一次
  pause & exit /b 1
)
echo [ok] 已推到 https://github.com/Doctor-182/diyic
pause
endlocal
