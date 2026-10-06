@echo off
rem =====================================================================
rem  yt2disc converter - web launcher for a phone
rem
rem  "yt2disc Web" listens on 127.0.0.1, which is this PC only.  This one
rem  listens on every interface, opens the firewall for the port and prints
rem  the address to type on the phone.  With cloudflared it also gets a
rem  public https URL, so the phone does not have to share your network.
rem
rem    yt2disc-web-public.cmd            your network AND a public URL
rem    yt2disc-web-public.cmd lan        your network only, no tunnel
rem    yt2disc-web-public.cmd fetch      download cloudflared into bin/ first
rem
rem  Optional environment overrides:
rem    PORT                  listen port          (default 8000)
rem    YT2DISC_WEB_HOST      bind address         (default 0.0.0.0)
rem    YT2DISC_WEB_TOKEN     your own shared key  (default: generated)
rem =====================================================================
setlocal EnableExtensions
title yt2disc converter (phone)

rem --- repo root = the folder this script lives in ----------------------
set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"
cd /d "%ROOT%"

rem --- Python interpreter (explicit path avoids the Store stub) ---------
set "PY=C:\Users\jythe\AppData\Local\Programs\Python\Python313\python.exe"
if not exist "%PY%" set "PY=python"

if not defined PORT set "PORT=8000"

rem --- the first argument picks the mode --------------------------------
set "MODE=--tunnel"
if /i "%~1"=="lan" set "MODE=--lan-only"
if /i "%~1"=="fetch" set "MODE=--tunnel --fetch-cloudflared"
if /i "%~1"=="nofirewall" set "MODE=--tunnel --no-firewall"

rem --- no browser here: the phone is the point, and the console is where
rem     the address and the key are printed ------------------------------
"%PY%" -m webapp._host %MODE% --port %PORT%
set "RC=%ERRORLEVEL%"

echo.
if not "%RC%"=="0" echo   yt2disc converter stopped with exit code %RC%.
echo   Press any key to close this window.
pause >nul

endlocal & exit /b %RC%
