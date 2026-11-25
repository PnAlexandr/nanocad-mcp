@echo off
setlocal
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" %*
set "result=%errorlevel%"
echo.
pause
exit /b %result%
