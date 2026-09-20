@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo Radar Workbench needs its local Python environment.
    echo Run these commands from this folder:
    echo   py -m venv .venv
    echo   .venv\Scripts\python.exe -m pip install -e ".[workbench]"
    pause
    exit /b 1
)
".venv\Scripts\python.exe" -c "import streamlit, numpy, matplotlib, serial" >nul 2>&1
if errorlevel 1 (
    echo Dependencies are missing. Run:
    echo   .venv\Scripts\python.exe -m pip install -e ".[workbench]"
    pause
    exit /b 1
)
".venv\Scripts\python.exe" -m streamlit run workbench\app.py
set "workbench_exit=%errorlevel%"
pause
exit /b %workbench_exit%
