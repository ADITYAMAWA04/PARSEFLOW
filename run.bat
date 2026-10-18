@echo off
cd /d "%~dp0"
if not exist .env copy .env.example .env >nul
if not exist .venv python -m venv .venv
call .venv\Scripts\activate.bat
pip install -q -r requirements.txt || (echo Installing requirements failed. & pause & exit /b 1)
python backend\start.py
pause
