@echo off
REM Broker web app + JSON API + still-image ingestion.
REM Local dev: auto-reloads when you edit code or templates (no manual restart).
REM Open http://localhost:8000 after it starts; Ctrl+C to stop.
REM Call the venv's python directly (robust if the project folder was renamed —
REM venv activate scripts bake in an absolute path and break after a move).
"%~dp0.venv\Scripts\python.exe" -m uvicorn webapp.app:app --host 0.0.0.0 --port 8000 --reload --reload-dir "%~dp0webapp" --reload-dir "%~dp0pipeline" --reload-include "*.html" %*
