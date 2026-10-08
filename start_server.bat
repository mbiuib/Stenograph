@echo off
cd /d "%~dp0"
echo === Stenograph server ===
echo Web UI:   http://127.0.0.1:8000
echo Bridge:   ws://^<this-machine-ip^>:8000/ws/^<meeting-id^>  (Jigasi dials this)
echo Note:     listening on 0.0.0.0 so the Jitsi VM can reach the bridge
echo Stop:     Ctrl+C in this window
echo.
if not "%STENOGRAPH_NO_BROWSER%"=="1" start "" /b cmd /c "timeout /t 4 /nobreak >nul && start http://127.0.0.1:8000"
".venv\Scripts\python.exe" -m uvicorn "stenograph.api.app:create_app" --factory --host 0.0.0.0 --port 8000
pause
