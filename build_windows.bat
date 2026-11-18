@echo off
setlocal
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
    echo Python was not found. Install Python 3.10 or newer from https://www.python.org/
    pause
    exit /b 1
)

python -m PyInstaller --version >nul 2>nul
if errorlevel 1 (
    echo Installing PyInstaller...
    python -m pip install pyinstaller
    if errorlevel 1 goto :failed
)

if not exist "assets\pictureviewer.ico" (
    echo Missing assets\pictureviewer.ico
    pause
    exit /b 1
)

echo Building PictureViewer.exe...
python -m PyInstaller --noconfirm --clean --onefile --windowed --name PictureViewer ^
    --icon "assets\pictureviewer.ico" ^
    --exclude-module numpy --exclude-module pytest --exclude-module pillow_heif ^
    picture_viewer.py

if errorlevel 1 goto :failed

echo.
echo Build complete: %~dp0dist\PictureViewer.exe
pause
exit /b 0

:failed
echo.
echo Build failed.
pause
exit /b 1
