@echo off
setlocal enabledelayedexpansion

:: Autodesk Fusion 360 Custom Protocol Launcher for Windows
:: Handles fusion360://open?file=http://...

set "URI=%~1"
if "%URI%"=="" (
    echo No URI provided.
    pause
    exit /b 1
)

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0fusion360_launcher.ps1" "%URI%"
