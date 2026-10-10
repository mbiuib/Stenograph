@echo off
rem Stop the whole Stenograph stack. Containers go away; data stays on the
rem host (bind mounts): ..\..\data, ..\..\certs, ..\jitsi-windows\config.
setlocal EnableExtensions
cd /d "%~dp0"

set "ENVF="
if exist "..\jitsi-windows\.env" set "ENVF=--env-file ..\jitsi-windows\.env"
if not defined ENVF if exist ".env" set "ENVF=--env-file .env"

docker compose %ENVF% -f docker-compose.yml down
pause
