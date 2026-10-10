@echo off
rem ==========================================================================
rem  Stenograph Jitsi on Windows - one-button deploy.
rem  Just double-click this file (run as Administrator once, optional, to add
rem  the LAN firewall rules). Steps:
rem    1. create .env if missing (random passwords + this PC's LAN address)
rem    2. make sure Docker Desktop is running (starts it if needed)
rem    3. add Windows Firewall rules (silently skipped without admin rights)
rem    4. issue a local-CA TLS certificate (needs Git for Windows; optional)
rem    5. docker compose up -d   (web, prosody, jicofo, jvb, jigasi)
rem    6. wait for the web page and print the URL
rem  For unattended runs set STENOGRAPH_NOPAUSE=1 (no pause prompts).
rem ==========================================================================
setlocal EnableExtensions
cd /d "%~dp0"

echo [1/6] Preparing .env ...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0prepare_env.ps1"
if errorlevel 1 goto :fail

set "JITSI_IP="
set "PUBLIC_URL="
for /f "usebackq tokens=1,* delims==" %%a in (".env") do (
    if "%%a"=="DOCKER_HOST_ADDRESS" set "JITSI_IP=%%b"
    if "%%a"=="PUBLIC_URL" set "PUBLIC_URL=%%b"
)
if "%JITSI_IP%"=="" set "JITSI_IP=127.0.0.1"

echo [2/6] Checking Docker Desktop ...
docker info >nul 2>&1
if not errorlevel 1 goto :docker_ok
echo       Docker Desktop is not running; starting it ...
if not exist "%ProgramFiles%\Docker\Docker\Docker Desktop.exe" goto :no_docker
start "" "%ProgramFiles%\Docker\Docker\Docker Desktop.exe"
echo       Waiting for the Docker engine, up to ~3 minutes ...
set /a WAITS=0
:wait_docker
ping -n 6 127.0.0.1 >nul
docker info >nul 2>&1
if not errorlevel 1 goto :docker_ok
set /a WAITS+=1
if %WAITS% GEQ 36 goto :no_engine
goto :wait_docker

:no_docker
echo       ERROR: Docker Desktop was not found on this PC.
echo       Install it first: https://www.docker.com/products/docker-desktop/
goto :fail

:no_engine
echo       ERROR: the Docker engine did not come up. Open Docker Desktop
echo       manually, wait for it to say "Engine running", then re-run.
goto :fail

:docker_ok

echo [3/6] Firewall rules for the LAN (need Administrator; skipped if not) ...
netsh advfirewall firewall add rule name="Stenograph Jitsi HTTPS" dir=in action=allow protocol=TCP localport=8443 >nul 2>&1
netsh advfirewall firewall add rule name="Stenograph Jitsi HTTP" dir=in action=allow protocol=TCP localport=8081 >nul 2>&1
netsh advfirewall firewall add rule name="Stenograph Jitsi JVB" dir=in action=allow protocol=UDP localport=10000 >nul 2>&1

echo [4/6] Folders and TLS certificate ...
mkdir "config\web\keys" 2>nul
mkdir "config\web\custom-ca" 2>nul
mkdir "config\prosody\custom-ca" 2>nul
mkdir "config\jicofo\custom-ca" 2>nul
mkdir "config\jvb\custom-ca" 2>nul
mkdir "config\transcriber\custom-ca" 2>nul
mkdir "config\storage\transcripts" 2>nul
if exist "config\web\keys\cert.crt" goto :certs_done

set "GITBASH="
if exist "%ProgramFiles%\Git\bin\bash.exe" set "GITBASH=%ProgramFiles%\Git\bin\bash.exe"
if not defined GITBASH if exist "%ProgramFiles%\Git\usr\bin\bash.exe" set "GITBASH=%ProgramFiles%\Git\usr\bin\bash.exe"
if not defined GITBASH if exist "%ProgramFiles(x86)%\Git\bin\bash.exe" set "GITBASH=%ProgramFiles(x86)%\Git\bin\bash.exe"
if not defined GITBASH if exist "%LOCALAPPDATA%\Programs\Git\bin\bash.exe" set "GITBASH=%LOCALAPPDATA%\Programs\Git\bin\bash.exe"
if not defined GITBASH goto :no_git

set "CERTDIR=%~dp0"
set "CERTDIR=%CERTDIR:\=/%"
echo       Issuing a certificate for %JITSI_IP% ...
"%GITBASH%" "%CERTDIR%make_cert.sh" %JITSI_IP%
if errorlevel 1 goto :cert_bad
echo       OK: clients should install  ca\ca.crt  (see README).
goto :certs_done

:no_git
echo       Git for Windows not found: HTTPS will use Jitsi's built-in
echo       self-signed certificate and browsers will warn. Install Git for
echo       Windows and re-run for a trusted local certificate.
goto :certs_done

:cert_bad
echo       WARNING: certificate generation failed; using Jitsi's built-in cert.

:certs_done

echo [5/6] Starting containers (the FIRST run downloads ~1.5 GB of images) ...
docker compose -f docker-compose.yml -f transcriber.yml up -d
if errorlevel 1 goto :fail

rem First-boot race: prosody registers the jigasi account a moment after
rem jigasi's first login attempt lands ("not-authorized" in its log). Wait a
rem little, and when the race shows up, restart the transcriber once.
ping -n 21 127.0.0.1 >nul
set "TRANSCRIBER_ID="
for /f "delims=" %%i in ('docker compose -f docker-compose.yml -f transcriber.yml ps -q transcriber 2^>nul') do set "TRANSCRIBER_ID=%%i"
if not defined TRANSCRIBER_ID goto :race_checked
docker logs --since 5m %TRANSCRIBER_ID% > "%TEMP%\stenograph-jigasi.log" 2>&1
findstr /C:"not-authorized" "%TEMP%\stenograph-jigasi.log" >nul
if errorlevel 1 goto :race_checked
echo       First-boot login race detected; restarting the transcriber ...
docker compose -f docker-compose.yml -f transcriber.yml restart transcriber >nul 2>&1
:race_checked
del "%TEMP%\stenograph-jigasi.log" >nul 2>&1

echo [6/6] Waiting for the web interface ...
set /a WAITS=0
:web_wait
ping -n 6 127.0.0.1 >nul
curl -sk -o nul --max-time 4 "https://127.0.0.1:8443/" >nul 2>&1
if not errorlevel 1 goto :web_ok
set /a WAITS+=1
if %WAITS% GEQ 24 goto :web_slow
goto :web_wait

:web_ok
echo.
echo   READY.  Open:  %PUBLIC_URL%
echo   Other machines on the LAN: run install-ca.bat (with ca\ca.crt next to
echo   it) once to avoid the certificate warning.
echo.
if "%STENOGRAPH_NOPAUSE%"=="" pause
exit /b 0

:web_slow
echo.
echo   Containers are up, but the web page did not answer yet.
echo   Check status:  docker compose -f docker-compose.yml -f transcriber.yml ps
echo   Try opening:   %PUBLIC_URL%
echo.
if "%STENOGRAPH_NOPAUSE%"=="" pause
exit /b 0

:fail
echo.
echo   FAILED. Common causes: Docker Desktop not ready; no internet for the
echo   first image download; port 8081/8443 taken (edit .env and re-run).
echo.
if "%STENOGRAPH_NOPAUSE%"=="" pause
exit /b 1
