@echo off
REM Ingest a live camera stream into the database.
REM Usage: run_stream.bat rtsp://user:pass@host:554/stream
call "%~dp0.venv\Scripts\activate.bat"
python -m pipeline.stream_runner %*
