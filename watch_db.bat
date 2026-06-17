@echo off
REM Live view of trucks as they get inserted — auto-refreshes every 3 seconds.
REM Press Ctrl+C to stop watching (does NOT stop the pipeline or the DB).
echo SELECT id, to_char(detected_at,'YYYY-MM-DD HH24:MI:SS') AS detected, source, license_plate AS plate, company_name AS company, phone_number AS phone FROM trucks ORDER BY id DESC LIMIT 20 \watch 3 | "%~dp0pgsql\bin\psql.exe" -U postgres -h localhost -p 5432 -d trucks
