@echo off
cd /d "%~dp0"
python main.py
if errorlevel 1 (
    echo.
    echo Application exited with an error. Check the output above.
    pause
)
