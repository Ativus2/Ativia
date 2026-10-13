@echo off
cd /d "%~dp0\..\.."
experiments\realtime_prototype\.venv\Scripts\python.exe experiments\realtime_prototype\launch.py --diagnostic
if errorlevel 1 pause
