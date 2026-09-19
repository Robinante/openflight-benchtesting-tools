@echo off
cd /d "%~dp0"
call .venv\Scripts\activate.bat
python -m streamlit run workbench\app.py
pause
