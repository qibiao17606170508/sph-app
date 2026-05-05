@echo off
title WeChat Channels Uploader
cd /d "%~dp0"

echo ==============================================
echo    WeChat Channels Uploader
echo ==============================================
echo.

echo Starting server...
%USERPROFILE%\AppData\Local\Programs\Python\Python311\python.exe run.py
pause
