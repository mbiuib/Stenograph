@echo off
cd /d "%~dp0"
echo === Stenograph HTTPS proxy (Caddy) ===
echo Local:       https://127.0.0.1/  ^(proxies the app on http://127.0.0.1:8000^)
echo LAN:         https://^<this-machine-ip^>/  ^(secure context required for Live capture^)
echo Certificate: certs\stenograph.crt  ^(override: TLS_CERT_FILE / TLS_KEY_FILE^)
echo Stop:        Ctrl+C in this window
echo.
set "CADDY=D:\Programs\caddy\caddy.exe"
if not exist "%CADDY%" (
  echo Caddy not found: %CADDY%
  echo Download it from https://caddyserver.com/download and put caddy.exe there.
  pause
  exit /b 1
)
if not exist "certs\stenograph.crt" (
  echo Certificate not found: certs\stenograph.crt
  echo Issue one from Git Bash:  scripts/make_cert.sh 192.168.0.9
  pause
  exit /b 1
)
"%CADDY%" run
pause
