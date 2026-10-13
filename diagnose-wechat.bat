@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo ERROR: Project .venv\Scripts\python.exe is missing. No dependencies were installed.
  exit /b 1
)
echo ZERO SEND MODE. Capturing fresh accessibility structure only.
".venv\Scripts\python.exe" -X utf8 "tools\wechat_recovery.py" diagnose %*
exit /b %errorlevel%
