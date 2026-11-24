@echo off
setlocal
cd /d "%~dp0.."
if not exist ".venv\Scripts\python.exe" (
  echo Project virtual environment missing. Create .venv and install requirements first.
  exit /b 1
)
call .venv\Scripts\activate.bat
python scripts\intranet.py %*
exit /b %errorlevel%
