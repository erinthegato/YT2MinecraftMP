@echo off
rem =====================================================================
rem  yt2disc converter - web launcher
rem  Starts the Flask/waitress server (webapp\app.py) then opens the UI.
rem  Use the "yt2disc Web" desktop shortcut, or run this file directly.
rem
rem  Optional environment overrides (defaults match webapp\app.py):
rem    PORT                  listen port          (default 8000)
rem    YT2DISC_WEB_HOST      bind address         (default 127.0.0.1)
rem    YT2DISC_NO_BROWSER=1  do not open a browser
rem =====================================================================
setlocal EnableExtensions
title yt2disc converter

rem --- repo root = the folder this script lives in ----------------------
set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"
cd /d "%ROOT%"

rem --- Python interpreter (explicit path avoids the Store stub) ---------
set "PY=C:\Users\jythe\AppData\Local\Programs\Python\Python313\python.exe"
if not exist "%PY%" set "PY=python"

rem --- defaults identical to webapp\app.py main() ------------------------
if not defined YT2DISC_WEB_HOST set "YT2DISC_WEB_HOST=127.0.0.1"
if not defined PORT set "PORT=8000"

rem --- 0.0.0.0 / :: are bind addresses, not browsable URLs -------------
set "BROWSE=%YT2DISC_WEB_HOST%"
if "%BROWSE%"=="0.0.0.0" set "BROWSE=127.0.0.1"
if "%BROWSE%"=="::" set "BROWSE=127.0.0.1"
set "URL=http://%BROWSE%:%PORT%/"

echo.
echo   yt2disc converter
echo   ------------------------------------------------------------
echo   folder : %ROOT%
echo   python : %PY%
echo   url    : %URL%
echo   stop   : Ctrl+C
echo.

rem --- open the UI once the server has had a moment to bind -------------
if not defined YT2DISC_NO_BROWSER start "" /b powershell -NoProfile -WindowStyle Hidden -Command "Start-Sleep -Seconds 2; Start-Process '%URL%'"

"%PY%" -m webapp.app
set "RC=%ERRORLEVEL%"

echo.
if not "%RC%"=="0" echo   yt2disc converter stopped with exit code %RC%.
echo   Press any key to close this window.
pause >nul

endlocal & exit /b %RC%
