@echo off
cd /d "%~dp0"
.venv\Scripts\python.exe main.py --paused
if errorlevel 1 pause
