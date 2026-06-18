@echo off
REM Call the venv's python directly (robust if the project folder was renamed —
REM venv activate scripts bake in an absolute path and break after a move).
"%~dp0.venv\Scripts\python.exe" "%~dp0main.py" %*
