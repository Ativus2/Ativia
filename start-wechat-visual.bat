@echo off
cd /d "%~dp0"
".venv\Scripts\python.exe" -X utf8 -B tools\wechat_visual.py run --ui
pause
