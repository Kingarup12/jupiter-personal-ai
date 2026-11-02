@echo off
cd /d "%~dp0\.."
echo Starting Jupiter Backend Server...
start "Jupiter Backend API" cmd /k "python -m app.server"

timeout /t 2 /nobreak >nul

cd /d "%~dp0"
echo Starting Jupiter UI...
python main.py
pause
