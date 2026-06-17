@echo off
REM Start the self-contained PostgreSQL (no Docker, no admin) on localhost:5432.
REM First run initializes the cluster and creates the 'trucks' database.
setlocal
set "PGBIN=%~dp0pgsql\bin"
set "PGDATA=%~dp0pgdata"

if not exist "%PGBIN%\pg_ctl.exe" (
  echo ERROR: portable Postgres not found at %~dp0pgsql
  echo Extract postgresql-*-windows-x64-binaries.zip here so that pgsql\bin exists.
  exit /b 1
)

if not exist "%PGDATA%\PG_VERSION" (
  echo Initializing database cluster...
  "%PGBIN%\initdb.exe" -D "%PGDATA%" -U postgres --auth=trust --encoding=UTF8 >nul
)

"%PGBIN%\pg_ctl.exe" -D "%PGDATA%" -o "-p 5432" -l "%~dp0pgsql.log" -w start
"%PGBIN%\createdb.exe" -U postgres -h localhost -p 5432 trucks 2>nul
echo PostgreSQL running on localhost:5432 (database: trucks)
