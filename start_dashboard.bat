@echo off
setlocal
cd /d "%~dp0"

if defined PYTHON_EXE goto check_python
if exist "%~dp0.venv\Scripts\python.exe" (
    set "PYTHON_EXE=%~dp0.venv\Scripts\python.exe"
    goto check_python
)
for /f "delims=" %%P in ('python -c "import sys; print(sys.executable)" 2^>nul') do set "PYTHON_EXE=%%P"
if defined PYTHON_EXE goto check_python
for /f "delims=" %%P in ('py -3 -c "import sys; print(sys.executable)" 2^>nul') do set "PYTHON_EXE=%%P"
if defined PYTHON_EXE goto check_python
echo Python not found. Install Python 3.10 or newer, or set PYTHON_EXE.
pause
exit /b 1

:check_python
"%PYTHON_EXE%" -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" 2>nul
if errorlevel 1 (
    echo Python 3.10 or newer is required. Check PYTHON_EXE: %PYTHON_EXE%
    pause
    exit /b 1
)
"%PYTHON_EXE%" -c "import requests, plotly" 2>nul
if errorlevel 1 (
    echo Missing dependencies. Run:
    echo "%PYTHON_EXE%" -m pip install -r requirements.txt
    pause
    exit /b 1
)

echo Python: %PYTHON_EXE%
echo Dashboard: http://127.0.0.1:8765/
echo Keep this window open while using the dashboard. Close it to stop the server.
"%PYTHON_EXE%" app.py --open-browser %*

if errorlevel 1 (
    echo Server stopped with an error. Check the message above.
    pause
)
