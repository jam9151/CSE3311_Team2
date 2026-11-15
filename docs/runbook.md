# MediaFlow runbook

How the live copy is run, updated, backed up and put back together. Written
after Iteration 3, when the checkout directory on the server turned out to
have been deleted while the container kept serving from the old image.

The live site is `https://mediaflow.jessemcnary.com`, served by the
`mediaflow-web` container on megatron behind the shared Caddy, using the
proxy overlay (`docker-compose.proxy.yml`).

## Where things are

| What | Where |
| --- | --- |
| Checkout | `/home/kirk-admin/projects/CSE3311_Team2` |
| Settings | `.env` in the checkout. Not in git. Keep a copy somewhere safe. |
| Database | Docker volume `mediaflow_mediaflow-data`, file `/data/db.sqlite3` inside the container |
| Container | `mediaflow-web` (and `mediaflow-push` when the push profile is on) |
| Health | `curl -s https://mediaflow.jessemcnary.com/healthz/` → `{"status": "ok"}` |
| Logs | `docker logs --tail 200 mediaflow-web` |

If the checkout is missing again: clone the repo to the path above, restore
`.env` from the safe copy, and run the deploy step. The database lives on the
volume, not in the checkout, so nothing is lost.

## Deploy

```bash
cd /home/kirk-admin/projects/CSE3311_Team2
git pull
docker compose -f docker-compose.yml -f docker-compose.proxy.yml up --build -d
docker compose logs --tail 50 web
```

Migrations run on container start. Watch the log for `Applying planner.00xx`
and then the gunicorn `Listening at` line, then open the site. Check that
`docker ps` shows `mediaflow-web` as `healthy` after about a minute.

If `.env` sets `COMPOSE_FILE`, plain `docker compose up --build -d` does the
same thing.

## Roll back

```bash
git log --oneline -5             # find the last good commit
git checkout <commit>
docker compose -f docker-compose.yml -f docker-compose.proxy.yml up --build -d
```

Migrations are not undone automatically. Every migration so far only adds
tables and columns, so an older build runs fine against a newer database.

## Back up

`docker/backup.sh` copies the database out of the running container with
SQLite's online backup, so it is safe while people are using the site, gzips
it, and keeps the last 14.

```bash
chmod +x docker/backup.sh              # once, after cloning
docker/backup.sh                       # to ~/mediaflow-backups
KEEP=30 docker/backup.sh /srv/backups  # elsewhere, keeping more
```

Nightly, as the user that runs Docker (`crontab -e`):

```
15 3 * * * /home/kirk-admin/projects/CSE3311_Team2/docker/backup.sh >> /home/kirk-admin/mediaflow-backup.log 2>&1
```

Copy the newest file off the machine now and then. A backup on the same disk
as the database is only half a backup.

## Restore

```bash
docker compose -f docker-compose.yml -f docker-compose.proxy.yml stop web
gunzip -k ~/mediaflow-backups/db-20261114-031500.sqlite3.gz
docker cp ~/mediaflow-backups/db-20261114-031500.sqlite3 mediaflow-web:/data/db.sqlite3
docker compose -f docker-compose.yml -f docker-compose.proxy.yml start web
```

Then open the site and check that recent data is there.

## Background push reminders

Off by default. To turn them on:

1. Generate keys once, on any machine with the project installed:

   ```bash
   python manage.py vapid_keys
   ```

2. Paste the two lines into `.env` on the server, add
   `VAPID_CLAIMS_EMAIL=someone@example.com` and `COMPOSE_PROFILES=push`.
3. Deploy as above. A second container, `mediaflow-push`, runs
   `manage.py send_push_reminders --loop` and checks every minute.
4. On My week, with reminders turned on and browser notifications allowed,
   the card says "Background reminders are on for this browser".

Generating new keys invalidates every existing subscription; browsers
re-subscribe the next time they load a page.

## PostgreSQL instead of SQLite

Only worth it if the course asks for more than one app container. Add
`docker-compose.postgres.yml` to `COMPOSE_FILE` and set `POSTGRES_PASSWORD`
in `.env`. The app reads `DATABASE_URL`, which the overlay sets, and ignores
the SQLite file.

To carry existing data across:

```bash
docker exec mediaflow-web python manage.py dumpdata --natural-foreign --natural-primary \
  -e contenttypes -e auth.permission -e sessions > dump.json
# switch COMPOSE_FILE to include the postgres layer, then:
docker compose up -d
docker cp dump.json mediaflow-web:/tmp/dump.json
docker exec mediaflow-web python manage.py loaddata /tmp/dump.json
```

Back it up with `docker exec mediaflow-db pg_dump -U mediaflow mediaflow | gzip > db.sql.gz`.

## Things that have gone wrong before

- **Checkout deleted, container still running** (Iteration 3). The container
  kept serving the old image. Fixed by re-cloning, restoring `.env`, and
  rebuilding on the existing volume. Hence the paths table above.
- **Secret key with `$` or `#` in `.env`.** Docker Compose reads those as
  variable and comment syntax. Generate keys with the command in the README.
- **Plain http on a LAN address.** Browsers drop Secure cookies, so sign-in
  fails. Set `DJANGO_SECURE_COOKIES=false` until HTTPS is in front.
