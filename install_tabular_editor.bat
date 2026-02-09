@echo off
REM Tabular Editor Quick Setup for Office Laptop
REM No admin privileges required

echo.
echo ========================================
echo Tabular Editor Quick Setup
echo ========================================
echo.

cd /d "%~dp0"

REM Check if Python is installed
python --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python is not installed!
    echo.
    echo Please install Python 3.10+ from:
    echo https://www.python.org/downloads/
    echo.
    pause
    exit /b 1
)

echo [1/2] Running installation script...
echo.

python setup_tabular_editor.py

if errorlevel 1 (
    echo.
    echo Installation failed. Please check the errors above.
    pause
    exit /b 1
)

echo.
echo [2/2] Verifying configuration...
echo.

REM Check if .env file was created
if exist ".env" (
    echo ✓ Configuration file created: .env
) else (
    echo ⚠️  Warning: .env file not found
)

echo.
echo ========================================
echo Setup Complete!
echo ========================================
echo.
echo Your system is now ready to generate PBIX files!
echo.
pause
