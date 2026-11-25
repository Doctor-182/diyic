@echo off
chcp 65001 >nul
rem 这个文件名是留给手滑和历史习惯的：真正干活的是同目录的 上传到GitHub.bat（可重复跑）
call "%~dp0上传到GitHub.bat" %*
