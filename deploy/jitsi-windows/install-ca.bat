@echo off
rem ============================================================================
rem  Install the "Stenograph Local CA" certificate into the Trusted Root store
rem  of the CURRENT USER (no admin rights needed).
rem
rem  Keep this file in the SAME FOLDER as ca.crt and run it by double-click.
rem  After it finishes: fully close ALL browser windows (including the tray)
rem  and reopen the Jitsi / Stenograph page (its https:// address).
rem ============================================================================
setlocal
set "CA=%~dp0ca.crt"

if not exist "%CA%" (
    echo.
    echo   ca.crt was not found next to this script.
    echo   Put install-ca.bat and ca.crt into one folder and run again.
    echo.
    pause
    exit /b 1
)

certutil -user -addstore -f Root "%CA%"
if errorlevel 1 (
    echo.
    echo   Installation FAILED. Copy the text above and send it back for help.
    echo.
    pause
    exit /b 1
)

echo.
echo   Done: the certificate is installed.
echo   Now FULLY close the browser (all windows) and reopen it,
echo   then open your Jitsi / Stenograph page again.
echo.
pause
