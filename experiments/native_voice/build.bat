@echo off
cd /d "%~dp0"
set "NODE=%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin\node.exe"
"%NODE%" node_modules\esbuild\bin\esbuild src\app.js --bundle --format=esm --outfile=dist\app.js
if errorlevel 1 pause
