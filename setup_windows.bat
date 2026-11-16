@echo off
cd /d "%~dp0"
echo Installing FFmpeg (if missing) and Python packages...
where ffmpeg >nul 2>nul || winget install -e --id Gyan.FFmpeg
python -m pip install -r requirements.txt
if not exist config.yaml copy config.example.yaml config.yaml
if not exist .env copy .env.example .env
echo.
echo Next: put your Claude API key in .env, then run:  python -m reelagent setup-instagram
pause
