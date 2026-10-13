@echo off
cd /d "%~dp0"
set PYTHONUTF8=1
if not exist ".venv\Scripts\python.exe" (
  echo Install the full requirements in .venv first. See README.md.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -m language_practice.launch
if errorlevel 1 pause
