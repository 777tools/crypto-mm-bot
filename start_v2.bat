@echo off
cd /d "%~dp0"
pip install python-bitbankcc requests >nul 2>&1
python bot_v2.py
pause
