@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0.."
title 检查 GitHub 连接
echo ==== 1. git ====
where git || (echo [x] 没装 Git & goto end)
git --version
echo.
echo ==== 2. 远端（走 SSH 443，绕开被重置的 443 TLS 直连）====
git remote -v
echo.
echo ==== 3. SSH 能不能通 ====
ssh -o BatchMode=yes -T git@github.com 2>&1 | findstr /i "successfully authenticated" >nul && (echo [ok] 已认证) || (echo [x] 公钥还没加到 GitHub 账号 —— 双击 scripts\上传到GitHub.bat)
echo.
echo ==== 4. HTTPS 直连状况（仅诊断，不用它推）====
powershell -NoProfile -Command "try { $null=Invoke-WebRequest https://github.com -UseBasicParsing -TimeoutSec 10; 'https 网页可访问' } catch { 'https 网页不通: ' + $_.Exception.Message }"
echo.
echo ==== 5. 没推的提交 ====
git log --oneline -10
:end
echo.
pause
