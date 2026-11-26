@echo off
chcp 65001 >nul
setlocal EnableExtensions
cd /d "%~dp0.."
call "scripts\_env.bat"
if errorlevel 1 ( pause & exit /b 1 )

echo ============================================
echo  打安装包：PyInstaller onedir + Inno Setup
echo  产物 dist\exe\VRDub\ 与 dist\installer\VRDub-<版本>-setup.exe
echo  包里不含任何密钥；模型 252 MB 内嵌，GGUF 三档默认不带
echo ============================================
echo.
%PY% "tools\build_exe.py" %*
if errorlevel 1 ( echo. & echo 打 exe 失败，看上面的报错。 & pause & exit /b 1 )

%PY% "tools\build_installer.py"
if errorlevel 1 ( echo. & echo 打安装包失败（多半是没装 Inno Setup，见 docs\打包.md）。 & pause & exit /b 1 )

echo.
echo 打完：
echo   dist\exe\VRDub\
echo   dist\installer\
pause
endlocal
