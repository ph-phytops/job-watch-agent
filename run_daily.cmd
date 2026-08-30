@echo off
rem Daily runner for job-watch-agent (called by Windows Task Scheduler).
rem %~dp0 = the folder containing this script, so the task works from anywhere.
cd /d "%~dp0"
".venv\Scripts\python.exe" jobwatch.py >> "data\run.log" 2>&1
