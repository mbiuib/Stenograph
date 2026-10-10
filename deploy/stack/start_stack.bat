@echo off
rem ==========================================================================
rem  Stenograph full stack in Docker: Jitsi + jigasi + server + ASR (GPU).
rem  Everything runs in ONE compose project ("stenograph-stack").
rem
rem  Prereqs:
rem    - Docker Desktop running (WSL2 backend + current NVIDIA driver for the
rem      ASR container's GPU access);
rem    - the standalone stand (deploy\jitsi-windows) and the native Stenograph
rem      server STOPPED — the stack reuses the same ports (443/8081/8443/10000).
rem
rem  ENV: uses ..\jitsi-windows\.env when present (the same jitsi passwords,
rem  ports and the jigasi whisper URL the standalone stand used — the shared
rem  config\ state stays loginable), otherwise .env next to this file.
rem  First run builds the image: long (CUDA torch), ~10-25 min + download.
rem ==========================================================================
setlocal EnableExtensions
cd /d "%~dp0"

set "ENVF="
if exist "..\jitsi-windows\.env" set "ENVF=--env-file ..\jitsi-windows\.env"
if not defined ENVF if exist ".env" set "ENVF=--env-file .env"

echo [1/3] Checking Docker Desktop ...
docker info >nul 2>&1
if errorlevel 1 (
    echo       Docker Desktop is not running.
    echo       Start it, wait for "Engine running", then re-run this file.
    pause
    exit /b 1
)

echo [2/3] Building the image and starting the stack (first build is long) ...
docker compose %ENVF% -f docker-compose.yml up -d --build
if errorlevel 1 goto :fail

echo [3/3] Waiting for the Stenograph server ...
set /a WAITS=0
:wait
ping -n 6 127.0.0.1 >nul
curl -sk -o nul --max-time 4 "https://127.0.0.1/api/health" >nul 2>&1
if not errorlevel 1 goto :ready
set /a WAITS+=1
if %WAITS% GEQ 20 goto :slow
goto :wait

:ready
echo.
echo   READY.
echo   Stenograph:  https://127.0.0.1/
echo   Jitsi:       https://^<this-pc-ip^>:8443/
echo   ASR service: http://127.0.0.1:9001/health  ^(loopback only^)
echo.
echo   Status:  docker compose %ENVF% -f docker-compose.yml ps
echo.
pause
exit /b 0

:slow
echo.
echo   Containers are up, but the server did not answer yet.
echo   Check:   docker compose %ENVF% -f docker-compose.yml ps
echo   Logs:    docker compose %ENVF% -f docker-compose.yml logs --tail 50 stenograph
echo.
pause
exit /b 0

:fail
echo.
echo   FAILED. Common causes: port 443/8081/8443/10000 already taken (stop the
echo   standalone stand and the native server first), Docker Desktop not ready,
echo   no internet for the first build.
echo.
pause
exit /b 1
