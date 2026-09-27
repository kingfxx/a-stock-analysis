@echo off
cd /d "%~dp0"

set "PYTHON_EXE=C:\Users\HINATA\AppData\Local\Programs\Python\Python311\python.exe"
if not exist "%PYTHON_EXE%" (
    echo Python not found: %PYTHON_EXE%
    pause
    exit /b 1
)

echo Dashboard: http://127.0.0.1:8765/
echo Keep this window open while using the dashboard. Close it to stop the server.
"%PYTHON_EXE%" app.py --open-browser

if errorlevel 1 (
    echo Server stopped with an error. Check the message above.
    pause
)
