@echo off
cd /d "%~dp0"
chcp 65001 >nul
if not exist ".venv\Scripts\pythonw.exe" (
  echo Ativia 运行环境缺失，请使用 setup-test.bat 检查安装。
  pause
  exit /b 1
)
start "" /b ".venv\Scripts\pythonw.exe" main.py
