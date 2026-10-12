@echo off
rem Runs Tuff Trucks Social with no window (started by start-social-hidden.vbs) and starts it again if it ever stops
rem unexpectedly. Startup output and crashes go to data\console.log. Stop it with stop-social.vbs.
cd /d "%~dp0"
rem Only this computer can connect directly; other devices use the Tailscale https address.
set "HOST=127.0.0.1"
if not exist data mkdir data
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
if not exist ".venv\Scripts\python.exe" (
  echo ===== %date% %time% .venv is missing: run start.bat once to set it up >> data\console.log
  exit /b 1
)
:run
echo ===== %date% %time% starting Tuff Trucks Social >> data\console.log
".venv\Scripts\python.exe" run.py >> data\console.log 2>&1
echo ===== %date% %time% stopped (exit %errorlevel%); starting again in 15 seconds >> data\console.log
timeout /t 15 /nobreak >nul
goto run
