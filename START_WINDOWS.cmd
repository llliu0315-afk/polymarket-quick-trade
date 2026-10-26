@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo First install: py -3 -m venv .venv
  echo Then: .venv\Scripts\python.exe -m pip install -r requirements-windows.txt
  pause
  exit /b 1
)
".venv\Scripts\python.exe" windows_start.py
if errorlevel 1 pause
endlocal
