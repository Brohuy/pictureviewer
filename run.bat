@echo off
setlocal
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
    echo Python was not found. Install Python 3.10 or newer from https://www.python.org/
    pause
    exit /b 1
)

python -c "import PIL" >nul 2>nul
if errorlevel 1 (
    echo Installing Pillow for the first run...
    python -m pip install -r requirements.txt
    if errorlevel 1 goto :install_failed
)

where pythonw >nul 2>nul
if errorlevel 1 (
    python "%~dp0picture_viewer.py" %*
    exit /b %errorlevel%
)

start "" pythonw "%~dp0picture_viewer.py" %*
exit /b 0

:install_failed
echo.
echo Could not install the dependencies.
pause
exit /b 1
