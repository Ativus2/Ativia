@echo off
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" goto install
py -3 -m venv .venv
if errorlevel 1 goto failed
:install
".venv\Scripts\python.exe" -m pip install -r requirements-text-only.txt
if errorlevel 1 goto failed
if not exist ".env" copy /y ".env.example" ".env" >nul
echo Ready. Configure .env, then run start-text-only.bat.
pause
exit /b
:failed
echo Setup failed. Check the error above. No audio packages are required for text-only mode.
pause
