@echo off
TITLE MediaVault Server
cd /d "d:\shortcut\server"
echo Starting MediaVault Server...
"d:\shortcut\server\venv\Scripts\python.exe" "d:\shortcut\server\server.py"
pause
