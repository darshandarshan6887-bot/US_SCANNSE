@echo off
cd /d "%~dp0"
echo Open  http://localhost:8000/main-dashboard/index.html  in your browser
echo (keep this window open; press Ctrl+C here to stop the server)
start "" http://localhost:8000/main-dashboard/index.html
python -m http.server 8000
