@echo off
cd /d "%~dp0"
.venv\Scripts\python.exe main.py --stop
if errorlevel 1 pause
