@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo ERROR: Project .venv\Scripts\python.exe is missing. No process was stopped.
  exit /b 1
)
".venv\Scripts\python.exe" -X utf8 "tools\wechat_recovery.py" stop
exit /b %errorlevel%
