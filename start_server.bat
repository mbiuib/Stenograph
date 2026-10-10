@echo off
rem ==========================================================================
rem  Stenograph server. Serves the web UI, API, SSE and the Jigasi bridge
rem  (wss://<this-machine-ip>/ws/<meeting-id>).
rem
rem  HTTPS: when certs\stenograph.crt + certs\stenograph.key exist the server
rem  serves TLS natively on port 443 - no proxy needed. Override the file
rem  paths with TLS_CERT_FILE / TLS_KEY_FILE. Without the files it falls back
rem  to plain HTTP on port 8000 (the bridge then is ws://...:8000/ws).
rem  After replacing the certificate files - restart this script.
rem ==========================================================================
setlocal EnableExtensions
cd /d "%~dp0"

set "CERT=certs\stenograph.crt"
set "KEY=certs\stenograph.key"
if not "%TLS_CERT_FILE%"=="" set "CERT=%TLS_CERT_FILE%"
if not "%TLS_KEY_FILE%"=="" set "KEY=%TLS_KEY_FILE%"

if not exist "%CERT%" goto :plain
if not exist "%KEY%" goto :plain

echo === Stenograph server (HTTPS) ===
echo Web UI:   https://127.0.0.1/   ^(from other machines: https://^<this-machine-ip^>/^)
echo Bridge:   wss://^<this-machine-ip^>/ws/^<meeting-id^>  - Jigasi dials this;
echo           the Jitsi side must trust the certificate (custom-ca, see README)
echo Cert:     %CERT%
echo Stop:     Ctrl+C in this window
echo.
if not "%STENOGRAPH_NO_BROWSER%"=="1" start "" /b cmd /c "timeout /t 4 /nobreak >nul && start https://127.0.0.1/"
".venv\Scripts\python.exe" -m uvicorn "stenograph.api.app:create_app" --factory --host 0.0.0.0 --port 443 --ssl-keyfile "%KEY%" --ssl-certfile "%CERT%"
pause
exit /b

:plain
echo === Stenograph server (HTTP: certificates not found) ===
echo Web UI:   http://127.0.0.1:8000
echo Bridge:   ws://^<this-machine-ip^>:8000/ws/^<meeting-id^>  - Jigasi dials this
echo Looked for: %CERT% + %KEY%
echo Issue one (Git Bash):  scripts/make_cert.sh ^<this-machine-ip^>
echo Note:     without HTTPS the browser microphone works only on localhost
echo Stop:     Ctrl+C in this window
echo.
if not "%STENOGRAPH_NO_BROWSER%"=="1" start "" /b cmd /c "timeout /t 4 /nobreak >nul && start http://127.0.0.1:8000"
".venv\Scripts\python.exe" -m uvicorn "stenograph.api.app:create_app" --factory --host 0.0.0.0 --port 8000
pause
