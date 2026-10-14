@echo off
setlocal
set "APP_ROOT=%~dp0."
chcp 65001 >nul
set "APP_OFFLINE="
set "APP_NO_PAUSE="
:arguments
if "%~1"=="" goto run_setup
if /i "%~1"=="--offline" (
    set "APP_OFFLINE=-Offline"
) else if /i "%~1"=="--no-pause" (
    set "APP_NO_PAUSE=1"
) else (
    echo Unknown option: %~1
    exit /b 2
)
shift
goto arguments
:run_setup
cd /d "%APP_ROOT%"
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%APP_ROOT%\scripts\setup.ps1" -Root "%APP_ROOT%" %APP_OFFLINE%
set "APP_EXIT=%errorlevel%"
echo Exit code: %APP_EXIT%
if defined APP_NO_PAUSE goto finished
pause
:finished
exit /b %APP_EXIT%
