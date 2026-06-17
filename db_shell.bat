@echo off
REM Open an interactive SQL shell on the trucks database.
REM Examples once inside:
REM   SELECT count(*) FROM trucks;
REM   SELECT id, detected_at, license_plate, company_name FROM trucks ORDER BY id DESC LIMIT 20;
REM   SELECT count(*) FROM trucks \watch 3      -- live-refresh a query
REM   \q                                        -- quit
"%~dp0pgsql\bin\psql.exe" -U postgres -h localhost -p 5432 -d trucks
