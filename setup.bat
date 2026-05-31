@echo off
echo ============================================================
echo  NDI Multicam Recorder ^| Dev Setup
echo ============================================================
echo.
echo NOTE: The NDI SDK runtime is NOT required.
echo       The ndi-python package bundles the NDI DLL directly.
echo.

python --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python not found.
    echo Install Python 3.11+ from https://python.org
    echo Check "Add Python to PATH" during installation.
    pause
    exit /b 1
)

echo Python found:
python --version
echo.

echo Installing dependencies...
echo   PyQt6, av (PyAV/FFmpeg), numpy, ndi-python, comtypes, python-osc
python -m pip install --upgrade pip
python -m pip install -r requirements.txt

echo.
echo ============================================================
echo  To run in development:   python main.py
echo  To build a distributable: build.bat
echo ============================================================
echo.
pause
