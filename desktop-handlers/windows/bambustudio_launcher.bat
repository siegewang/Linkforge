@echo off
setlocal enabledelayedexpansion

:: Bambu Studio Custom Protocol Launcher for Windows
:: Handles bambustudio://open?file=http://...

set "URI=%~1"
if "%URI%"=="" (
    echo No URI provided.
    pause
    exit /b 1
)

:: Run PowerShell launcher with the argument
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0bambustudio_launcher.ps1" "%URI%"
