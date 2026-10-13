@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" goto missing
start "" /b ".venv\Scripts\pythonw.exe" main.py --voice
exit /b
:missing
echo Ativia voice environment missing. Run setup-test.bat first.
pause
