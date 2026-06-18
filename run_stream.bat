@echo off
REM Ingest a live camera stream into the database.
REM Usage: run_stream.bat rtsp://user:pass@host:554/stream
REM Call the venv's python directly (robust if the project folder was renamed —
REM venv activate scripts bake in an absolute path and break after a move).
"%~dp0.venv\Scripts\python.exe" -m pipeline.stream_runner %*
