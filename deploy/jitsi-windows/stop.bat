@echo off
rem ==========================================================================
rem  Stop the Stenograph Jitsi stack (containers are removed; .env, config/
rem  and images are kept, so start.bat brings everything back quickly).
rem ==========================================================================
setlocal EnableExtensions
cd /d "%~dp0"
echo Stopping the Stenograph Jitsi stack ...
docker compose -f docker-compose.yml -f transcriber.yml down
echo Done.
if "%STENOGRAPH_NOPAUSE%"=="" pause
