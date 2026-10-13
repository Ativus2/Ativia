@echo off
cd /d "%~dp0"
chcp 65001 >nul
if not exist ".venv\Scripts\python.exe" (
  echo Run setup-test.bat first.
  pause
  exit /b 1
)
set QT_QPA_PLATFORM=offscreen
set PYTHONUTF8=1
".venv\Scripts\python.exe" -m unittest discover -s tests -v
if errorlevel 1 (
  echo Tests failed. Please keep the output for diagnosis.
  pause
  exit /b 1
)
echo All offline tests passed.
pause
