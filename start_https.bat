@echo off
chcp 65001 >nul
rem Локальный HTTPS-прокси для «Стенографа»: Caddy слушает :443 и проксирует
rem на приложение (127.0.0.1:8000). Сертификат — certs\stenograph.crt
rem (замена без правки кода; см. README, раздел «HTTPS и сертификаты»).
setlocal
set "CADDY=D:\Programs\caddy\caddy.exe"
if not exist "%CADDY%" (
  echo Caddy не найден: %CADDY%
  echo Скачайте с https://caddyserver.com/download и положите caddy.exe туда.
  pause
  exit /b 1
)
if not exist "%~dp0certs\stenograph.crt" (
  echo Нет сертификата certs\stenograph.crt — см. README, раздел «HTTPS».
  echo Быстрый выпуск из Git Bash: scripts/make_cert.sh 192.168.0.9
  pause
  exit /b 1
)
cd /d "%~dp0"
"%CADDY%" run
