@echo off
REM start.bat — one-click startup for Windows
REM Double-click or run from cmd

echo Starting ASOS Wealth Platform...
cd /d %~dp0\backend

IF NOT EXIST venv (
    echo Creating Python virtual environment...
    python -m venv venv
)

call venv\Scripts\activate.bat
pip install -r requirements.txt -q

IF NOT EXIST ..\\.env (
    copy ..\\.env.example ..\\.env
    echo Created .env from template
)

echo.
echo Backend starting at http://localhost:8000
echo Login page: http://localhost:8000/static/login.html
echo.

python -m uvicorn main:app --reload --port 8000 --host 0.0.0.0
pause
