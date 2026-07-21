@echo off
cd /d "%~dp0"
pip install requests >nul 2>&1
python demo.py
pause
