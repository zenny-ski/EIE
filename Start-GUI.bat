@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo Setting up the virtual environment...
    python -m venv .venv || goto :error
    ".venv\Scripts\python" -m pip install -q -r requirements.txt || goto :error
)
".venv\Scripts\python" -m eie gui
goto :eof

:error
echo Setup failed. Make sure Python 3.10+ is installed and on PATH.
pause
