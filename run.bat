@echo off
REM Runs the monitor using the venv created by setup.bat.
cd /d "%~dp0"
call venv\Scripts\activate.bat
python -m artnet_monitor.main --config config\patch.yaml %*
