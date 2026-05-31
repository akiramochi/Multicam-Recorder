@echo off
setlocal
cd /d "%~dp0"

echo ============================================================
echo  NDI Multicam Recorder ^| Build
echo ============================================================
echo.

:: Find pyinstaller on PATH first, then fall back to the user Scripts folder
where pyinstaller >nul 2>&1
if not errorlevel 1 (
    set PYINSTALLER=pyinstaller
    goto :found
)
for /f "delims=" %%i in ('python -c "import sys,os; print(os.path.join(os.path.dirname(sys.executable),'Scripts','pyinstaller.exe'))" 2^>nul') do set PYINSTALLER=%%i
if exist "%PYINSTALLER%" goto :found

echo ERROR: PyInstaller not found. Run:  pip install pyinstaller
pause
exit /b 1

:found
echo Using: %PYINSTALLER%
echo.

echo Cleaning previous build...
if exist build  rmdir /s /q build
if exist dist   rmdir /s /q dist

echo.
echo Running PyInstaller...
echo.
"%PYINSTALLER%" multicam_recorder.spec --noconfirm

if errorlevel 1 (
    echo.
    echo BUILD FAILED. See errors above.
    pause
    exit /b 1
)

echo.
echo ============================================================
echo  Build complete!
echo  Output: dist\NDI Multicam Recorder\
echo.
echo  The folder is fully self-contained — no Python, no NDI SDK,
echo  no other installs required on the target machine.
echo.
echo  To distribute: zip the "NDI Multicam Recorder" folder.
echo ============================================================
echo.
pause
