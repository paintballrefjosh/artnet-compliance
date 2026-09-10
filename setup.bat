@echo off
REM One-time setup: creates an isolated Python virtual environment in .\venv
REM and installs all dependencies into it. Does not touch system Python.

cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
    echo python was not found on PATH. Install it from python.org first,
    echo and make sure "Add python.exe to PATH" is checked during install.
    exit /b 1
)

python -m venv venv
call venv\Scripts\activate.bat
python -m pip install --upgrade pip
pip install -r requirements.txt

echo.
echo Setup complete.
echo Next: edit config\patch.yaml (set listen_ip to this machine's real LAN IP),
echo open the firewall port (see README.md), then run: run.bat
