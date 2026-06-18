@echo off
REM Broker web app + JSON API + still-image ingestion.
REM Open http://localhost:8000 after it starts.
REM Call the venv's python directly (robust if the project folder was renamed —
REM venv activate scripts bake in an absolute path and break after a move).
"%~dp0.venv\Scripts\python.exe" -m uvicorn webapp.app:app --host 0.0.0.0 --port 8000 %*
