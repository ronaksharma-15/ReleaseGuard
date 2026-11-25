@echo off
setlocal
cd /d "%~dp0"
where python >nul 2>nul
if errorlevel 1 (
  echo Install Python 3.10 or newer, then run this launcher again.
  pause
  exit /b 1
)
python -c "import json, urllib.request; assert json.load(urllib.request.urlopen('http://127.0.0.1:8787/api/state', timeout=1)).get('mode') == 'local'" >nul 2>nul
if not errorlevel 1 (
  start "" "http://127.0.0.1:8787/"
  exit /b 0
)
python app.py --open
if errorlevel 1 pause
