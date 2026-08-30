@echo off
rem Daily runner for job-watch-agent (called by Windows Task Scheduler).
rem %~dp0 = the folder containing this script, so the task works from anywhere.
rem uv provisions Python and syncs .venv on its own; it must be on the PATH
rem of the account running the scheduled task.
cd /d "%~dp0"
uv run --frozen jobwatch.py >> "data\run.log" 2>&1
