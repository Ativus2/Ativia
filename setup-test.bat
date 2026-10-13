@echo off
cd /d "%~dp0"
chcp 65001 >nul
if exist ".venv\Scripts\python.exe" goto install
py -3.12 -m venv .venv
if errorlevel 1 (
  echo Please install 64-bit Python 3.12 with the Python launcher, then retry.
  pause
  exit /b 1
)
:install
".venv\Scripts\python.exe" -m pip install -r requirements-tested.txt
if errorlevel 1 (
  echo Dependency installation failed. Check the error above and retry.
  pause
  exit /b 1
)
if not exist ".env" copy /y ".env.example" ".env" >nul
echo Setup complete. Edit .env and enter your own API key, then run start.bat.
pause
