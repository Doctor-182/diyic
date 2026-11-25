@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion
cd /d "%~dp0.."
title 上传到 GitHub（SSH 443，可重复跑）
set "URL=git@github.com:Doctor-182/diyic.git"
set "PUB=%USERPROFILE%\.ssh\id_ed25519.pub"

echo ==========================================
echo  推这个仓库到 GitHub（不是一次性的，随时可跑）
echo ==========================================
echo.
where git >nul 2>nul || (echo [x] 没装 Git for Windows & pause & exit /b 1)
if not exist ".git" (echo [x] 当前目录不是仓库 & pause & exit /b 1)

git remote get-url origin 2>nul | findstr /i "git@" >nul
if errorlevel 1 (
  git remote set-url origin %URL% 2>nul || git remote add origin %URL%
  echo [*] 远端已设为 %URL%
)

if not exist "%PUB%" (
  echo [x] 没找到 SSH 公钥 %PUB%
  pause & exit /b 1
)

:check
ssh -o BatchMode=yes -T git@github.com 2>&1 | findstr /i "successfully authenticated" >nul
if errorlevel 1 goto addkey
echo [ok] GitHub 认这条路了，开始推送...
echo.
git push -u --force origin main
if errorlevel 1 (echo. & echo [x] 推送失败，把上面的报错发我 & pause & exit /b 1)
echo.
echo [ok] 传完了：https://github.com/Doctor-182/diyic
echo      以后改了代码双击 scripts\推上去.bat 就行。
pause
exit /b 0

:addkey
echo [!] 这台机器的 SSH 公钥还没加到你的 GitHub 账号。
echo     公钥已复制到剪贴板，马上打开添加页面：直接 Ctrl+V 粘到 Key 框，Title 随便写。
echo.
type "%PUB%"
echo.
clip < "%PUB%"
start "" https://github.com/settings/ssh/new
echo 加完回来这里按任意键继续...
pause >nul
goto check
