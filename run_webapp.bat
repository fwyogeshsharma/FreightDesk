@echo off
REM Broker web app + JSON API + still-image ingestion.
REM Local dev: auto-reloads when you edit code or templates (no manual restart).
REM Open http://localhost:8000 after it starts; Ctrl+C to stop.
REM Requires the Postgres container to be running first (docker compose up -d db).
REM --env-file loads .env: DATABASE_URL (must use 127.0.0.1, not localhost, on Windows —
REM localhost resolves to IPv6 ::1 but Docker publishes IPv4 only) and ADMIN_PASSWORD.
REM Call the venv's python directly (robust if the project folder was renamed —
REM venv activate scripts bake in an absolute path and break after a move).
"%~dp0.venv\Scripts\python.exe" -m uvicorn webapp.app:app --host 0.0.0.0 --port 8000 --env-file "%~dp0.env" --reload --reload-dir "%~dp0webapp" --reload-dir "%~dp0pipeline" --reload-include "*.html" %*
