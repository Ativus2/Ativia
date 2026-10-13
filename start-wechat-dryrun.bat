@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo ERROR: Project .venv\Scripts\python.exe is missing. No dependencies were installed.
  exit /b 1
)
echo ZERO SEND MODE. This checks current transport readiness; it never sends messages.
".venv\Scripts\python.exe" -X utf8 "tools\wechat_recovery.py" start %*
exit /b %errorlevel%
