@echo off
cd /d "%~dp0"
pip install -r requirements.txt >nul 2>&1
python bot_v2.py --mode sim
pause
