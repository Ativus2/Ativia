@echo off
cd /d "%~dp0"
echo Open http://127.0.0.1:9884 then click Start. No microphone before clicking.
echo Ctrl+C stops this broker; pause/end or close the page to close its direct WebRTC call.
..\realtime_prototype\.venv\Scripts\python.exe server.py --port 9884 --session-limit 3
if errorlevel 1 pause
