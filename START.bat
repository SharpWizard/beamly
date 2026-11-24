@echo off
title Beamly
cd /d "%~dp0"
python -c "import flask, webview, waitress, qrcode, PIL" 2>nul || (
  echo Installing Beamly requirements...
  python -m pip install -r requirements.txt
)
start "" pythonw app.py
