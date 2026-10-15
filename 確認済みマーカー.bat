@echo off
setlocal DisableDelayedExpansion
chcp 65001 >nul
set "APP_ROOT=%~dp0"
powershell.exe -NoLogo -NoProfile -Command "if ([IO.Path]::GetFullPath($env:APP_ROOT).TrimEnd([char]92).Length -gt 40) { Write-Host '配置先が長すぎます。E:\ROI-OCRなど短い場所へ移してください。'; exit 1 }"
if errorlevel 1 exit /b 1
if not exist "%APP_ROOT%runtime\python.exe" (
  echo 初期設定.batを先に実行してください。
  exit /b 1
)
if "%~1"=="" goto choose_excel
if /I "%~1"=="--no-pause" goto choose_excel
"%APP_ROOT%runtime\python.exe" -I -B -X utf8 "%APP_ROOT%source\run.py" mark --root "%APP_ROOT%." --excel "%~1"
goto finished
:choose_excel
"%APP_ROOT%runtime\python.exe" -I -B -X utf8 "%APP_ROOT%source\run.py" mark --root "%APP_ROOT%."
:finished
set "APP_RESULT=%ERRORLEVEL%"
if /I NOT "%~1"=="--no-pause" if /I NOT "%~2"=="--no-pause" pause
exit /b %APP_RESULT%
