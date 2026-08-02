@echo off
set "MONITOR_ROOT=%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%MONITOR_ROOT%stop.ps1" %*
