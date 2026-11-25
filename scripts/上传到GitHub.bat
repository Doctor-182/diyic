@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0.."
echo.
echo  ==================================================
echo   上传到 GitHub：Doctor-182/diyic
echo  ==================================================
echo.
echo  走 HTTPS + 本机代理（2080 那个 sing-box），先自动探一遍端口。
echo  第一次会问一次 token：网页打开下面这行，勾 repo，生成后粘进来。
echo.
echo    https://github.com/settings/tokens/new?scopes=repo^&description=VRDub
echo.
echo  token 只写进 %USERPROFILE%\.git-credentials，不进仓库、不进安装包。
echo  之后每次双击本脚本都不用再填。
echo.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0push.ps1" -Attempts 40 -GapSec 8 -Force
echo.
if errorlevel 1 (
  echo  [x] 没推上去。看 out\push.log 最后几行的报错：
  echo      - auth problem  =^> 删掉 ~/.git-credentials 里 github 那行再跑
  echo      - no proxy      =^> 代理没开，先开 sing-box 再跑
) else (
  echo  [v] 推上去了：https://github.com/Doctor-182/diyic
)
echo.
pause
