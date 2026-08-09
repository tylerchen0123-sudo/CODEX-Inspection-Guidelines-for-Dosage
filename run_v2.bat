@echo off
cd /d "%~dp0"
for /f "tokens=5" %%a in ('netstat -ano ^| findstr /i ":8910" ^| findstr /i "LISTENING"') do taskkill /F /PID %%a
set "PY=C:/Users/Administrator/.workbuddy/binaries/python/versions/3.13.12/python.exe"
if not exist "%PY%" set "PY=python"
"%PY%" server_v2.py --port 8910 --interval 3
pause
