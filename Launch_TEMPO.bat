@echo off
echo Booting TEMPO Dashboard...

:: Ensure the sandbox exists before trying to run
IF NOT EXIST ".venv\Scripts\activate.bat" (
    echo [ERROR] Sandbox not found. Please run "Setup_Windows.bat" first.
    pause
    exit /b 1
)

:: Activate the sandbox and launch the GUI
call .venv\Scripts\activate
python tempo_gui.py