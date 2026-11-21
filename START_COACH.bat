@echo off
rem Double-click to start the whole AI Coach server on this PC (Docker, one box per person, gateway, public address).
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "deploy\local\start_all.ps1"
echo.
pause
