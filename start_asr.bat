@echo off
rem ==========================================================================
rem  Stenograph ASR service: whisper + moss behind an OpenAI-compatible API
rem  (GET /v1/models, POST /v1/audio/transcriptions, GET /health) on :9001.
rem
rem  Runs in a SEPARATE process so heavy inference can never freeze the main
rem  server's API. The main server keeps working with its built-in engines;
rem  pointing it at this service is the next step (MEETSCRIBE_ASR_BACKEND).
rem
rem  Port: MEETSCRIBE_ASR_PORT (default 9001). Stop: Ctrl+C in this window.
rem ==========================================================================
setlocal EnableExtensions
cd /d "%~dp0"
if "%MEETSCRIBE_ASR_PORT%"=="" set "MEETSCRIBE_ASR_PORT=9001"

echo === Stenograph ASR service ===
echo Models: http://127.0.0.1:%MEETSCRIBE_ASR_PORT%/v1/models
echo Health: http://127.0.0.1:%MEETSCRIBE_ASR_PORT%/health
echo Stop:   Ctrl+C in this window
echo.

".venv\Scripts\python.exe" -m stenograph.asr_service
pause
