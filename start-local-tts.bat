@echo off
cd /d "%~dp0"
if not exist "local_tts\.venv\Scripts\python.exe" (
 echo Local TTS environment missing. See local_tts\REPORT.md.
 pause
 exit /b 1
)
"local_tts\.venv\Scripts\python.exe" -u "local_tts\launch.py"
if errorlevel 1 (
 echo Local TTS could not start. See local_tts\logs\server-console.log.
 pause
)
