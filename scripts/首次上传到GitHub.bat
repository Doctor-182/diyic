@echo off
chcp 65001 >nul
setlocal EnableExtensions
cd /d "%~dp0.."

echo ============================================================
echo   第一次上传到 GitHub（Doctor-182/diyic）
echo   说明：本地历史从 0.9.0-dev 起，和远端那个只有 README 的
echo         "Initial commit" 没有共同祖先，所以第一次要用 --force 覆盖它。
echo         那个 README 只有两行字（# diyic / just a first try）。
echo   第一次会弹出浏览器让你登录 GitHub 并授权，点完就记住了，
echo   以后双击「推上去.bat」就行，不用再登录。
echo ============================================================
echo.

git remote get-url origin >nul 2>nul
if errorlevel 1 (
  git remote add origin https://github.com/Doctor-182/diyic
  echo [i] 已添加 remote origin
)

echo [i] 将要上传的提交：
git log --oneline | findstr /n "^" | findstr /b "1: 2: 3: 4: 5:"
echo.

git push -u --force origin main
if errorlevel 1 (
  echo.
  echo [x] 没推上去。最常见的原因：
  echo     1) 浏览器授权窗口被关掉了 -^> 再双击本文件一次
  echo     2) GitHub 账号没有这个仓库的写权限 -^> 确认仓库是你的
  echo     3) 网络 -^> 挂代理后重试
  pause
  exit /b 1
)

echo.
echo [ok] 推完了：https://github.com/Doctor-182/diyic
echo      以后改完代码双击 scripts\推上去.bat 即可。
pause
endlocal
