@echo off
:: Self-elevation check
net session >nul 2>&1
if %errorLevel% neq 0 (
    echo [3DPrintLib] Requesting Administrator privileges to register URI protocols...
    powershell -Command "Start-Process '%~f0' -Verb RunAs"
    exit /b
)

setlocal enabledelayedexpansion
set "SCRIPT_DIR=%~dp0"
set "BS_BAT=%SCRIPT_DIR%bambustudio_launcher.bat"
set "F360_BAT=%SCRIPT_DIR%fusion360_launcher.bat"

echo ========================================================
echo   3DPrintLib Desktop Protocol Handler Registration
echo ========================================================
echo.
echo Registering 'bambustudio://' handler -> "%BS_BAT%"
reg add "HKCR\bambustudio" /ve /t REG_SZ /d "URL:Bambu Studio Protocol" /f >nul
reg add "HKCR\bambustudio" /v "URL Protocol" /t REG_SZ /d "" /f >nul
reg add "HKCR\bambustudio\shell\open\command" /ve /t REG_SZ /d "\"%BS_BAT%\" \"%%1\"" /f >nul

echo Registering 'fusion360://' handler -> "%F360_BAT%"
reg add "HKCR\fusion360" /ve /t REG_SZ /d "URL:Autodesk Fusion 360 Protocol" /f >nul
reg add "HKCR\fusion360" /v "URL Protocol" /t REG_SZ /d "" /f >nul
reg add "HKCR\fusion360\shell\open\command" /ve /t REG_SZ /d "\"%F360_BAT%\" \"%%1\"" /f >nul

echo.
echo [SUCCESS] Both 'bambustudio://' and 'fusion360://' protocols are registered!
echo Clicking "Open in Bambu Studio" or "Open in Fusion 360" in your browser
echo will now directly launch your desktop application with the model loaded.
echo.
pause
