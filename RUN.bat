@echo off
title TorrentForge - torrent builder
cd /d "%~dp0"
echo Starting TorrentForge...
echo A browser window will open at http://127.0.0.1:8777/
echo Close this window (or press Ctrl+C) to stop the server.
echo.
python app.py %*
if errorlevel 1 (
  echo.
  echo TorrentForge exited with an error. Is Python on your PATH?
  pause
)
