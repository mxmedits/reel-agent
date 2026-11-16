@echo off
REM Double-click to start the Reel Agent. Put a shortcut to this file in shell:startup to run at login.
cd /d "%~dp0"
python -m reelagent watch
pause
