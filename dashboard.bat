@echo off
chcp 65001 >nul
echo  CryptoMM Dashboard: http://localhost:9999/
start http://localhost:9999/
wsl -e php -S 0.0.0.0:9999 -t "/mnt/c/Users/ryu/Dropbox/Claudツール/Claudcodeで作成版/crypto-mm-bot/web"
pause
