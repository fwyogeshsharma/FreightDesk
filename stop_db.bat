@echo off
REM Stop the portable PostgreSQL started by start_db.bat.
setlocal
set "PGBIN=%~dp0pgsql\bin"
set "PGDATA=%~dp0pgdata"
"%PGBIN%\pg_ctl.exe" -D "%PGDATA%" -m fast stop
