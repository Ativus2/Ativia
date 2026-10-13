@echo off
cd /d "%~dp0"
experiments\realtime_prototype\.venv\Scripts\python.exe experiments\desktop_pet\main.py --open-panel
if errorlevel 1 pause
