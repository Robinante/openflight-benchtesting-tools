@echo off
REM OpenFlight Radar Workbench -- offline analysis of saved captures.
REM Needs: pip install streamlit matplotlib
cd /d "%~dp0\.."
python -m streamlit run workbench\app.py
