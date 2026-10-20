@echo off
start "Mosquitto broker" "C:\Program Files\mosquitto\mosquitto.exe" -c "C:\Program Files\mosquitto\mosquitto.conf" -v
timeout /t 2 >nul
cd /d "%~dp0"
python preflight.py