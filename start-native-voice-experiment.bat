@echo off
cd /d "%~dp0"
echo EXPERIMENT: Native voice uses the model's built-in voice, not GPT-SoVITS.
experiments\realtime_prototype\.venv\Scripts\python.exe experiments\desktop_pet\main.py
if errorlevel 1 pause
