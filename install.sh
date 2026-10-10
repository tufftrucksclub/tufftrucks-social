#!/usr/bin/env bash
# One-time setup on Linux / Raspberry Pi OS / macOS.  Run:  bash install.sh
set -e
cd "$(dirname "$0")"
if command -v apt-get >/dev/null 2>&1; then
  echo "Installing ffmpeg (for TikTok videos)…"
  sudo apt-get update -qq && sudo apt-get install -y -qq ffmpeg python3-venv
elif command -v brew >/dev/null 2>&1; then
  brew install ffmpeg
fi
python3 -m venv .venv
. .venv/bin/activate
pip install --upgrade pip -q
pip install -r requirements.txt -q
echo "Installing background removal (optional, may take a few minutes)…"
pip install -r requirements-cutout.txt -q || echo "  Skipped — photos will be framed instead of cut out."
[ -f .env ] || cp .env.example .env
echo
echo "Done. Next:  edit .env   then   .venv/bin/python run.py"
echo "Tip: set DEMO_MODE=1 in .env to click around with sample data first."
