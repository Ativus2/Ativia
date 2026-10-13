@echo off
cd /d "%~dp0"
".venv\Scripts\python.exe" -B tools\wechat_visual.py stop
pause
