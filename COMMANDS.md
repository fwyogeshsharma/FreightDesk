# FreightDesk — Command Cheat Sheet

Everything you need to run, deploy, and operate FreightDesk, in one place.
Your environment values are filled in already:

| Thing | Value |
|---|---|
| Live app | http://34.31.185.19:8090 |
| GCP project | `agile-airship-198614` |
| VM instance | `rolling-expense-prod` |
| VM zone | `us-central1-a` |
| VM home | `/home/fabercomp_gmail_com` |
| Web login | username `admin` + your `ADMIN_PASSWORD` (the seed admin) |

> On the **VM** use `sudo docker-compose` (note the hyphen — it's Compose v1 there).
> On your **laptop (Windows)** use the `.bat` scripts and `gcloud`.

---

## 1. Local development (Windows laptop)

```bat
start_db.bat                         :: start the portable Postgres (only if not using Docker)
run_webapp.bat                       :: start the web app -> http://localhost:8000 (auto-reloads on edits)
```
Create / upgrade the local database schema (from the project folder):
```bat
.venv\Scripts\python.exe scripts\init_db.py
.venv\Scripts\python.exe scripts\migrate_report_fields.py
.venv\Scripts\python.exe scripts\migrate_user_accounts.py
```
Process a video locally (into the local DB):
```bat
run.bat --input videos --sink db          :: whole videos\ folder
run.bat --input "D:\path\to\clip.mp4" --sink db   :: one file
```
Create an operator account locally:
```bat
.venv\Scripts\python.exe scripts\create_user.py create --username asha --role telecaller --name "Asha"
```

---

## 2. Deploy / update the VM

SSH into the VM (GCP Console → Compute Engine → VM instances → **SSH**), then:
```bash
cd ~/FreightDesk
git pull
sudo docker-compose up -d --build                 # rebuild + restart web (+ db)
```
Run any pending migrations (idempotent — safe to re-run every deploy):
```bash
sudo docker-compose run --rm web python scripts/init_db.py
sudo docker-compose run --rm web python scripts/migrate_report_fields.py
sudo docker-compose run --rm web python scripts/migrate_user_accounts.py
```

---

## 3. Operate the VM (status, logs, restart)

```bash
cd ~/FreightDesk
sudo docker-compose ps                 # what's running + health
sudo docker-compose logs -f web        # tail web app logs (Ctrl+C to stop tailing)
sudo docker-compose logs -f db         # tail database logs
sudo docker-compose restart web        # restart just the web app
sudo docker-compose up -d              # start everything (after a stop)
sudo docker-compose down               # stop the app + db (data is kept in the volume)
free -h                                # check memory / swap
```

---

## 4. Manage operator accounts (telecallers / admins)

Run on the VM (`sudo docker-compose run --rm web ...`) or locally (`.venv\Scripts\python.exe ...`).
Operators log in to the web app by **username**; contributors are separate (mobile, by phone).
```bash
sudo docker-compose run --rm web python scripts/create_user.py create --username asha --role telecaller --name "Asha"
sudo docker-compose run --rm web python scripts/create_user.py list
sudo docker-compose run --rm web python scripts/create_user.py set-role     --user asha --role admin
sudo docker-compose run --rm web python scripts/create_user.py set-password --user asha
sudo docker-compose run --rm web python scripts/create_user.py deactivate   --user asha
sudo docker-compose run --rm web python scripts/create_user.py activate     --user asha
```

---

## 5. Process a video on the VM

**a) Upload the video from your laptop** (Windows cmd, needs the gcloud SDK). Use the
absolute remote path and `--quiet` to skip the host-key prompt:
```cmd
gcloud compute scp "D:\path\to\clip.mp4" rolling-expense-prod:/home/fabercomp_gmail_com/FreightDesk/videos/ --zone=us-central1-a --project=agile-airship-198614 --quiet
```
(No gcloud on the laptop? Use the GCP Console browser-SSH ⚙ → **Upload file**, then on the VM
`mv ~/clip.mp4 ~/FreightDesk/videos/`.)

**b) Run the extraction** (writes trucks straight into the DB; refresh the app to see them):
```bash
cd ~/FreightDesk
sudo docker-compose --profile pipeline run --rm pipeline                      # whole videos/ folder
sudo docker-compose --profile pipeline run --rm pipeline --input /app/videos/clip.mp4 --sink db   # one file
```
**c) Stop it mid-run:** press **Ctrl+C** in that terminal (again to force). From another terminal:
```bash
sudo docker ps -q --filter "name=pipeline" | xargs -r sudo docker stop
```
> Keep the default single worker on this VM (do NOT add `--workers 2` — it will run out of memory).
> Trucks already found are saved as it goes; stopping only halts further processing.

---

## 6. GCP / VM info (no special permissions needed — run on the VM)

```bash
curl -s -H "Metadata-Flavor: Google" http://metadata.google.internal/computeMetadata/v1/project/project-id; echo
curl -s -H "Metadata-Flavor: Google" http://metadata.google.internal/computeMetadata/v1/instance/zone; echo
```
> Don't run `gcloud` on the VM (its service account lacks scope) and never prefix gcloud with `sudo`.
> Open a firewall port (run from your **laptop** or the Console):
> `gcloud compute firewall-rules create NAME --allow tcp:PORT --source-ranges 0.0.0.0/0 --project=agile-airship-198614`

---

## 7. Handy database queries

Open a psql shell on the VM:
```bash
cd ~/FreightDesk
sudo docker-compose exec db psql -U postgres -d trucks
```
Reward-eligible contributors (reports a telecaller PASSED):
```sql
SELECT reported_by, count(*) approved
FROM trucks WHERE source='image_api' AND review_status='PASSED'
GROUP BY reported_by ORDER BY approved DESC;
```
Possible abuse (lots of unverified submissions):
```sql
SELECT reported_by, count(*) total,
       count(*) FILTER (WHERE status='UNVERIFIED') unverified
FROM submission_log GROUP BY reported_by ORDER BY unverified DESC;
```
Row counts:
```sql
SELECT count(*) FROM trucks;
SELECT review_status, count(*) FROM trucks WHERE source='image_api' GROUP BY review_status;
```
(Exit psql with `\q`.)

---

## 8. Mobile API — curl examples (for testing)

Base URL `http://34.31.185.19:8090`. Full contract in **`API_CONTRACT.md`**, live docs at
**`/docs`**. The mobile app uses its own HTTP client, but these `curl`s exercise the exact
same endpoints for testing.

> Run these in **Git Bash** or **on the VM** (single-quoted JSON, `\` line-continuation).
> In **Windows cmd**: put each on one line and escape inner quotes, e.g.
> `-d "{\"phone\":\"9811008120\",\"password\":\"secret123\"}"`.

**Register a contributor** (always role `contributor`) → returns `{token, user}`:
```bash
curl -i -X POST http://34.31.185.19:8090/api/auth/register \
  -H "Content-Type: application/json" \
  -d '{"phone":"9811008120","password":"secret123","display_name":"Ravi"}'
```

**Login** → returns `{token, user}`:
```bash
curl -i -X POST http://34.31.185.19:8090/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"phone":"9811008120","password":"secret123"}'
```

**Save the token** in a shell variable for the calls below (Git Bash / VM):
```bash
TOKEN=$(curl -s -X POST http://34.31.185.19:8090/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"phone":"9811008120","password":"secret123"}' | sed -n 's/.*"token":"\([^"]*\)".*/\1/p')
echo "$TOKEN"
```
(or just copy the `token` value from the login response and use it directly below.)

**Who am I / validate token:**
```bash
curl -i -H "Authorization: Bearer $TOKEN" http://34.31.185.19:8090/api/auth/me
```

**My submissions + reward status:**
```bash
curl -i -H "Authorization: Bearer $TOKEN" http://34.31.185.19:8090/api/auth/me/reports
```

**Submit a truck report — anonymous** (works without login; uses `-F` multipart):
```bash
curl -i -F phone_number=9811008120 \
  -F images=@D:/projects/FreightDesk/test_images/truck.png \
  http://34.31.185.19:8090/api/trucks/report
```

**Submit a truck report — logged in** (attributed to the account; up to 5 `images`, plus optional fields):
```bash
curl -i -H "Authorization: Bearer $TOKEN" \
  -F images=@D:/projects/FreightDesk/test_images/truck.png \
  -F images=@D:/projects/FreightDesk/test_images/truck2.png \
  -F phone_number=9928001122 \
  -F vehicle_number=RJ14CA1234 \
  -F loaded_status=loaded \
  -F number_of_wheels=12 \
  -F location="NH-48, Jaipur" \
  -F latitude=26.9124 -F longitude=75.7873 \
  http://34.31.185.19:8090/api/trucks/report
```

**Logout** (revoke the token):
```bash
curl -i -X POST -H "Authorization: Bearer $TOKEN" http://34.31.185.19:8090/api/auth/logout
```

> `@path` uploads a **file**; without `@` curl sends the literal text. In Git Bash use a
> lowercase drive (`/d/...`) or the `D:/...` form shown above. Report fields: `images`
> (1–5, required) and `phone_number` (required) are the only musts; the rest are optional.
