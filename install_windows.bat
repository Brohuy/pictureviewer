@echo off
setlocal
cd /d "%~dp0"

if not exist "dist\PictureViewer.exe" (
    echo PictureViewer.exe was not found.
    echo Run build_windows.bat first, or pass the executable path to this script.
    pause
    exit /b 1
)

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\install_windows.ps1" %*
if errorlevel 1 (
    echo.
    echo Installation failed.
    pause
    exit /b 1
)

exit /b 0
