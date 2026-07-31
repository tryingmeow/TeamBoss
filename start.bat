@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1" %*
set "ERR=%ERRORLEVEL%"

echo.
if not "%ERR%"=="0" (
  echo Start failed with exit code %ERR%.
  pause
  exit /b %ERR%
)

echo Services were launched. Close the Backend/Frontend windows to stop them.
pause
exit /b 0
