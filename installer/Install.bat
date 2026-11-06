@echo off
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1"
echo.
echo (If the installer stopped unexpectedly, the details are in install.log in this folder.)
pause
