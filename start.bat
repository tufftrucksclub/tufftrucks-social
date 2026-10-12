@echo off
REM Windows: first run sets everything up, after that it just starts the app.
cd /d "%~dp0"
rem Only this computer can connect directly; other devices use the Tailscale https address.
set "HOST=127.0.0.1"
where ffmpeg >nul 2>nul || echo ffmpeg not found - TikTok videos need it. Install with:  winget install Gyan.FFmpeg
if not exist .venv (
  py -3 -m venv .venv
  .venv\Scripts\python -m pip install --upgrade pip
  .venv\Scripts\pip install -r requirements.txt
  .venv\Scripts\pip install -r requirements-cutout.txt
)
if not exist .env copy .env.example .env
.venv\Scripts\python run.py
pause
