@echo off
title moomoo-agent dashboard (local only)
cd /d "%~dp0"

set "PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if not exist "%PY%" set "PY=python"

echo Starting a local-only server on 127.0.0.1:8765 ...
start "moomoo-agent dashboard server" "%PY%" -m http.server 8765 --bind 127.0.0.1
timeout /t 2 /nobreak >nul
start "" "http://127.0.0.1:8765/dashboard/index.html"

echo.
echo Dashboard opened in your default browser.
echo Keep the "moomoo-agent dashboard server" window open while using it.
echo Close that window to stop the server. Nothing is exposed to the internet.
timeout /t 5 /nobreak >nul
