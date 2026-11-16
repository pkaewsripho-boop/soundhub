@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Thai Voice Studio
set PY=
where py >nul 2>nul && set PY=py -3
if not defined PY (where python >nul 2>nul && set PY=python)
if not defined PY (
  echo.
  echo [!] Python not found. Install Python 3.10+ from https://www.python.org/downloads/
  echo     IMPORTANT: tick "Add python.exe to PATH" during install, then run this file again.
  echo.
  pause
  exit /b 1
)
if not exist ".venv\Scripts\python.exe" (
  echo Setting up for the first time, please wait...
  %PY% -m venv .venv || (echo [!] Could not create virtual environment & pause & exit /b 1)
)
".venv\Scripts\python.exe" -m pip install -q --disable-pip-version-check -r requirements.txt || (echo [!] Install failed. Check internet connection. & pause & exit /b 1)
".venv\Scripts\python.exe" server.py %*
pause
