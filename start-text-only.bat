@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" goto missing
start "" /b ".venv\Scripts\pythonw.exe" main.py --text-only
exit /b
:missing
echo Ativia environment missing. Run setup-text-only.bat first.
pause
