@echo off
REM Broker web app + JSON API + still-image ingestion.
REM Open http://localhost:8000 after it starts.
call "%~dp0.venv\Scripts\activate.bat"
python -m uvicorn webapp.app:app --host 0.0.0.0 --port 8000 %*
