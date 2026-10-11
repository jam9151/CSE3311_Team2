# MediaFlow

CSE 3311 Team 2.

A Django app that keeps films, TV, books and games in one backlog, looks at the
gaps in your week, and tells you what actually fits the time you have tonight.

The competitors all cover one medium and none of them read your calendar.
Letterboxd shows a runtime but never uses it for anything. Goodreads can tell
you you're behind but not when you have time to catch up. We take the schedule
as input and answer the question people actually have: what do I start tonight?

## Running it

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python manage.py migrate
python manage.py seed_catalog --demo
python manage.py runserver
```

Then go to `http://127.0.0.1:8000/`. Nothing needs configuring to run locally.
After pulling changes, run the `pip install` line again in case a package was
added.

`seed_catalog` loads 36 titles into the catalog. The `--demo` flag also makes a
test account with a full class/work schedule already filled in, which saves
having to enter one every time you want to try the scheduler:

- user `demo`, password `mediaflow123`

Signing up for real needs a `@mavs.uta.edu` address, and you have to confirm it
before the app opens up (see Email below). The demo account is already
confirmed. Admin is at `/admin/`
(make a superuser with `python manage.py createsuperuser`).

Tests: `python manage.py test`

## Styles (Tailwind)

The CSS is built with Tailwind. The built file, `static/css/app.css`, is
committed, so you only need Node if you're changing how things look.

```powershell
npm install
npm run css          # rebuilds on save while you work on templates
npm run build:css    # minified build, run this before committing
```

Edit `static/src/app.css`, never `static/css/app.css` directly, because the
next build overwrites it. The colours live in the `@theme` block there
(`bg-surface`, `text-muted`, `border-line` and so on), and the shared bits
like `.btn`, `.card` and `.pill` are defined there too.

If you add a Tailwind class to a template and it doesn't show up, the CSS
probably hasn't been rebuilt yet.

## Docs

`docs/specification-and-design.md` is the write-up: inputs and outputs, data
structures, use cases and screen transitions. `docs/specification-and-design.pdf`
is the same thing built for handing in.

To rebuild the PDF after editing the markdown:

```powershell
python docs/build_pdf.py
```

It needs Chrome or Edge, the `markdown` package, and internet, because the
mermaid diagrams are drawn by a real browser.

## Where we are on the feature list

| Feature | Status |
| --- | --- |
| User accounts | done, `@mavs.uta.edu` only, confirmed by email link |
| Media search | done, filter by type / genre / length, paged, real titles via `import_catalog` |
| Media details | done, shows whether it fits your free time today |
| Visual calendar | done, month grid + day panel |
| Weekly schedule | done, recurring blocks and one-off commitments, both editable |
| Priority system | done, 1-5, editable from the backlog list |
| Free time detection | done |
| Accept/reject suggestions | done, rejections feed back into the scoring |
| Plan the week | done, one click plans the next 7 days, accept one by one or all at once |
| Review manager | done |
| Progress | done, time logged, streaks, finished counts, breakdown by type |
| Notifications | partly done, reminders while a tab is open, no background push yet |

## Growing the catalog

`seed_catalog` loads 36 hand-entered titles. `import_catalog` pulls real ones
from free sources, from a handful up to about 10,000 per type:

```powershell
python manage.py import_catalog --only books    # works right away, no key
python manage.py import_catalog                 # everything you have a key for
python manage.py import_catalog --limit 20      # a quick try, 20 per type
python manage.py import_catalog --dry-run       # list what it would add
```

| Type | Source | Key |
| --- | --- | --- |
| Books | [Open Library](https://openlibrary.org/developers/api) | none |
| Movies and TV | [TMDB](https://developer.themoviedb.org/) | free, `TMDB_API_KEY` |
| Games | [RAWG](https://rawg.io/apidocs) | free, `RAWG_API_KEY` |

Put the keys in `.env` (see `.env.example`). No restart needed, the command
reads the file each time. A type with no key is skipped with a message and the
rest still run. TMDB gives you both a short "API Key" and a long "API Read
Access Token", either one works.

Everything ends up as one number, `typical_minutes`, because that is all the
scheduler needs:

| Type | How the time is worked out |
| --- | --- |
| Movie | TMDB runtime, 40 to 300 minutes |
| TV | episodes times episode length, shows over about 400 hours are skipped |
| Book | pages times 1.3 minutes (`MINUTES_PER_PAGE`) |
| Game | RAWG's average hours played, 1 to 300 hours |

It is safe to run again. Each imported row remembers where it came from, so a
second run refreshes it instead of adding a copy, which also means edits you
make to an imported row in the admin are overwritten. A title you entered by
hand, or that came from the seed data, wins over an imported one with the same
title, type and year. If a source fails partway, what was saved before the
problem stays saved.

### A very large catalog

Step by step:

1. Get the two free keys. TMDB (movies and TV): sign up at
   https://www.themoviedb.org/signup, then open
   https://www.themoviedb.org/settings/api and request a key. Copy the short
   "API Key". RAWG (games): https://rawg.io/apidocs, then get a key.
2. Put them in `.env`:

   ```text
   TMDB_API_KEY="paste-the-tmdb-key"
   RAWG_API_KEY="paste-the-rawg-key"
   ```

3. Try a handful of each first, to check the keys work:

   ```powershell
   python manage.py import_catalog --limit 5
   ```

4. Then the big runs. Do the types separately so one failing doesn't hold up
   the others. `--skip-existing` is what makes a long run safe to stop and
   carry on:

```powershell
python manage.py import_catalog --only movies tv --limit 10000 --skip-existing
python manage.py import_catalog --only games --limit 10000 --skip-existing
python manage.py import_catalog --only books --limit 2000 --skip-existing
```

With `--skip-existing`, titles already in the catalog are passed over without
asking the source again, and they count toward `--limit`. So `--limit 10000`
means "get me to 10,000 of this type", and running the same command again after
an interruption carries on from where it stopped. It still has to walk the
source's list from the top to find where it was, which costs a few minutes at
this size but no per-title requests. Without the flag, a rerun refreshes every
title it meets, which is slower.

How far each source goes:

- **Movies and TV.** TMDB refuses any page past 500, so each of its lists gives
  at most 10,000. The importer walks three side by side, top rated, popular and
  most voted, and skips what it has already seen. TMDB's top rated TV list is
  only about 2,600 shows long, which is why the third list is there. Talk shows,
  news, soaps, shows with no episode length and shows over about 400 hours are
  left out, which cuts roughly a fifth of TV, so expect TV to land near 10,000
  rather than exactly on it. Movies lose very few.
- **Speed.** Details are fetched six at a time for TMDB and three at a time for
  RAWG. A live test of 300 movies and 300 shows took 43 seconds, so 10,000 of
  each should take on the order of half an hour. Games have not been timed.
- **Games.** RAWG's free key is limited per month. Third party sources say
  20,000 requests, which I could not confirm. The importer spends one request
  per game kept plus one per 40 listed, and skips the request for a game the
  list already shows has no playtime. 10,000 games is about half that, so don't
  run it twice from scratch. `--skip-existing` makes a rerun free.
- **Books.** Open Library asks for about three requests a second at most, so
  books are fetched one at a time and are the slowest. The genre searches yield
  low thousands of usable books.

Each person imports into their own `db.sqlite3`, which git ignores, so every
teammate and any deployed copy has to run the import themselves.

Book genres come from Open Library's subject tags and are approximate. Game
playtimes are what RAWG's players report, so a few are off. Catalog data is
credited in the footer, which TMDB's and RAWG's terms ask for.

## Email

New accounts get a confirmation link by email and are held on a "check your
email" page until they open it. Links are signed, last 3 days, and stop working
if the address changes. "Send it again" is limited to once a minute. Accounts
that existed before this was added were marked confirmed by the migration, and
staff accounts are never held back.

Nothing needs a mail server to run locally. By default Django prints the email
in the terminal where `runserver` is running, so copy the link from there.

To send real mail, copy the example settings file and fill it in:

```powershell
copy .env.example .env
```

`.env` is read when the server starts, so restart it after editing. It is
ignored by git, so the password stays on your machine. Everyone on the team
makes their own, or shares one project account's details privately, never in
the repo. A variable set in the terminal beats the same one in `.env`.

For Gmail, `EMAIL_HOST_PASSWORD` must be an app password, not the account's
normal password. Turn on 2-Step Verification, then Google Account, Security,
App passwords. Google shows it in groups of four letters and spaces are fine.
Other providers work too, set the host, user and password they give you.
`EMAIL_PORT` (587) and `EMAIL_USE_TLS` (true) have defaults.

To confirm someone by hand, tick `email_verified` on their profile in the
admin.

To test sign-up over and over, wipe the accounts between tries:

```powershell
python manage.py reset_accounts
```

It lists who it will delete, asks you to type `yes`, and removes those accounts
along with their backlogs and schedules. Staff accounts and the catalog are
kept. Add `--keep-demo` to spare the demo account, `--yes` to skip the prompt,
or `--include-staff` to remove admins too. It refuses to run with `DEBUG` off.

## Reminders

Turn them on from the Reminders card on My week and pick how many minutes of
warning you want. Anything accepted onto your calendar, and any one-off
commitment, then shows up in two places when it is about to start: a banner on
Today, and a browser notification if you've allowed them.

This is not true push. `static/js/reminders.js` asks `/notifications/upcoming/`
once a minute, so reminders only fire while a MediaFlow tab is open. Reminding
someone with the browser closed needs a service worker, VAPID keys, the
`pywebpush` package and something on the server running on a timer. That is the
next step if we want it.

## Progress

The Progress page shows time logged this month, things finished, your current
and best streak, your average review rating, a 30 day chart of time per day,
and how this month's time splits across movies, TV, books and games.

All of it comes from sessions you mark **done**, on the day you marked them.
Minutes you type into a backlog item by hand have no date, so they can't be
placed on a day and are not counted. A streak is a run of days with at least
one done session, and it stays alive through a quiet morning, so it only
drops to zero once a whole day has passed with nothing.

The chart is plain HTML and CSS with a small script (`static/js/stats.js`) for
the hover and keyboard tooltips, and every number in it is also in the "Show
as a table" list and the legend. The four type colours are set in the `@theme`
block of `static/src/app.css` as `viz-movie`, `viz-tv`, `viz-book` and
`viz-game`. They are different from the pill colours on purpose: the pill
colours are pale and two of them are too alike to tell apart as bars. These
four were checked for colour blind separation and contrast against the card
background. Keep one colour per type if you change them.

## How the scheduling works

Two steps, both in `planner/services.py`.

`free_intervals()` figures out when you're free. You enter when you're *busy*
(classes, shifts, one-off stuff) and it subtracts all of that from your waking
hours, plus anything you've already accepted. What's left are the gaps. Gaps
under 20 minutes get thrown away.

`suggest_for_day()` then picks something for each gap. It scores every backlog
item on priority, whether you've already started it, how much of the gap it
uses, and whether you'd finish it in one go. Turning something down subtracts
from its score for the next two weeks so it stops nagging you about it.

The part that makes the output feel right is `SESSION_RULES` in `models.py`.
Movies are all or nothing - if the runtime doesn't fit the gap we don't suggest
it at all. Books, TV and games get split into whole chunks (a chapter, an
episode) and capped at one sitting. That cap matters: without it the first item
picked would take the entire free day, which is how it behaved at first.

So a 2 hour gap in the morning gets you a chapter, and a long free afternoon
gets the movie.

"Plan my week" on My week runs `suggest_for_week()`, which calls
`suggest_for_day()` for each of the next seven days in order and tells each day
what the earlier ones already took. That is what stops a film being planned
twice or a book being planned past its last page, and each earlier sitting of
an item costs it `variety_penalty` points so the week isn't the same title
seven times. Anything already accepted counts as planned. The new suggestions
are pending until you add them, one at a time or with "Add all".

### How suggestions are scored

Every number is in one place, `ScoringWeights` in `planner/services.py`, and
the defaults are `DEFAULT_WEIGHTS`. They only mean anything next to each
other, so the table says where each one sits in steps of one priority level.

| Weight | Default | What it does, and why that size |
| --- | --- | --- |
| `per_priority_step` | 20 | Points per priority level, so 20 to 100. The user's own say-so is the biggest term, and one level is the unit the rest are measured in. |
| `in_progress_bonus` | 25 | Finishing beats starting. A bit over one step: a started 3 beats a fresh 4, a fresh 5 still beats a started 3. |
| `gap_fill_bonus` | 30 | Times the share of the gap used. Prefers filling a gap. Between one and two steps, so it reorders neighbours but can't lift an item two levels. |
| `whole_film_bonus` | 15 | A film that fits is the best use of a long evening. A film also always earns the finishing bonus, so it leads by 35. |
| `finishes_item_bonus` | 20 | The sitting would finish the item. Exactly one step. |
| `rejection_penalty` | 35 | Per "Not today" inside the memory window. Over one step, under two: one no drops a 4 below a fresh 3 but not below a fresh 2. |
| `rejection_memory_days` | 14 | How long a no counts. It is a penalty, not a filter, so the item comes back. |
| `variety_penalty` | 15 | Per sitting already planned this week. Under one step, so a priority buys one repeat before the next title catches up. |

`ScoringTradeoffTests` in `planner/tests.py` pins each of those relationships
and `RealisticDaysTests` pins a whole student week. If you change a weight and
one of them fails, the sentence in the table (and in the code comment above the
weight) has stopped being true, so fix whichever is wrong.

To try other numbers without touching the defaults, pass your own:

```python
from dataclasses import replace
from planner.services import DEFAULT_WEIGHTS, suggest_for_week

gentler = replace(DEFAULT_WEIGHTS, rejection_penalty=10)
suggest_for_week(user, weights=gentler, persist=False)   # persist=False saves nothing
```

Two things worth knowing about the defaults:

- A film that fits leads by 35 points, which is more than it sounds. A priority
  2 film beats a priority 3 chapter, and can edge a priority 4 chapter when it
  fills the gap better. If films win more often than you want, lower
  `whole_film_bonus`.
- A gap gets one suggestion however long it is. A free Saturday with no
  commitments is one 15 hour gap, so it gets one suggestion.

## Running in Docker

The image runs the app the way production does: gunicorn, hashed static files,
debug off, and the SQLite database on a volume called `mediaflow-data`. You need
Docker Desktop, or Docker Engine with the Compose plugin.

1. Put a secret key in `.env`, next to the other settings:

   ```bash
   python -c "import secrets; print(secrets.token_urlsafe(50))"
   ```

   ```text
   DJANGO_SECRET_KEY="paste-it-here"
   ```

2. Build and start it, then open http://localhost:8000:

   ```bash
   docker compose up --build
   ```

   The first build downloads Python and installs the packages, which takes a few
   minutes. Later builds are quick. Add `-d` to run it in the background.

3. Everything else is run inside the container. Create an admin, load the
   starter catalog and the demo account, or import the big catalog (your API
   keys come from the same `.env`):

   ```bash
   docker compose exec web python manage.py createsuperuser
   docker compose exec web python manage.py seed_catalog --demo
   docker compose exec web python manage.py import_catalog --only movies tv --limit 10000 --skip-existing
   ```

Day to day:

```bash
docker compose logs -f web      # watch the log
docker compose down             # stop it, the database is kept
docker compose up --build       # start again, rebuilding after a code change
```

The container is named `mediaflow-web`, so plain Docker commands work too, for
example `docker logs mediaflow-web` or `docker exec -it mediaflow-web python manage.py shell`.
The `docker compose` commands above use the service name, `web`, and are unaffected.

**Your data.** The database lives in the `mediaflow-data` volume, so it
survives `down`, rebuilds and restarts. `docker compose down -v` deletes it.
To take a copy:

```bash
docker compose cp web:/data/db.sqlite3 backup.sqlite3
```

To bring in the database you already have on your machine, for example after a
big local import:

```bash
docker compose cp db.sqlite3 web:/data/db.sqlite3
docker compose exec --user root web chown app:app /data/db.sqlite3
docker compose restart web
```

The `chown` is needed because a copied file belongs to root, and the app
deliberately doesn't run as root.

### On a remote server

`.env` is ignored by git, so a fresh clone on a server doesn't have one and
`docker compose` will stop with "DJANGO_SECRET_KEY is missing". Make it there:

```bash
cd ~/projects/CSE3311_Team2
echo "DJANGO_SECRET_KEY=\"$(python3 -c 'import secrets; print(secrets.token_urlsafe(50))')\"" >> .env
chmod 600 .env
nano .env
```

That adds a fresh key without you having to copy one around. In the editor,
add the address people will type into the browser, or the app answers 400:

```text
DJANGO_ALLOWED_HOSTS="megatron,192.168.1.50"
```

Also add the email, `TMDB_API_KEY` and `RAWG_API_KEY` lines from your own
`.env` if you want confirmation emails and catalog imports to work there.

If you reach the server over plain http, which is normal before HTTPS is set
up, also add `DJANGO_SECURE_COOKIES=false`. Without it the browser throws away
the sign-in cookie and every form fails with a CSRF error. Turn it back to
`true` once HTTPS is in front. Then:

```bash
sudo docker compose up -d --build
```

```bash
sudo docker compose logs -f web
```

If the page won't load from another machine, check the server's firewall and
your router. One trap: Docker publishes ports straight past `ufw`, so a
firewall rule won't hide port 8000. `BIND_ADDRESS=127.0.0.1` in `.env` is what
keeps the app off the network. For a real domain with HTTPS, see the next
section.

### HTTPS on a real domain

This sets the site up at `mediaflow.jessemcnary.com`. Swap in your own domain
throughout. `docker-compose.caddy.yml` adds [Caddy](https://caddyserver.com/) in
front of the app. Caddy gets the HTTPS certificate from Let's Encrypt, renews it
by itself, and sends plain http to https. The app is no longer reachable
directly, only through Caddy.

Before you start:

1. **DNS.** Add an `A` record for `mediaflow.jessemcnary.com` pointing at the
   server's public IP address. If Cloudflare proxies the record, set its
   SSL/TLS mode to Full (strict). In Flexible mode Cloudflare talks to the server
   over http, Caddy redirects it to https, and the site loops forever.
2. **Ports.** Ports 80 and 443 must reach the server from the internet, which
   usually means forwarding them on the router. Let's Encrypt needs that to
   prove the domain is yours. Nothing else on the server can already be using
   them, such as an existing nginx or Apache.

Then put these in the server's `.env`. Take out `DJANGO_SECURE_COOKIES=false`
if you added it earlier, because HTTPS makes it unnecessary:

```text
DJANGO_ALLOWED_HOSTS="mediaflow.jessemcnary.com"
DJANGO_CSRF_TRUSTED_ORIGINS="https://mediaflow.jessemcnary.com"
DJANGO_BEHIND_PROXY=true
DJANGO_SSL_REDIRECT=true
MEDIAFLOW_DOMAIN=mediaflow.jessemcnary.com
COMPOSE_FILE=docker-compose.yml:docker-compose.caddy.yml
```

`COMPOSE_FILE` makes plain `docker compose` include the Caddy file, so you
don't have to type `-f` twice. Start it and watch Caddy fetch the certificate:

```bash
sudo docker compose up -d --build
```

```bash
sudo docker compose logs -f caddy
```

Look for a line saying the certificate was obtained, then open
`https://mediaflow.jessemcnary.com`. Once that works, turn on HSTS by adding
`DJANGO_HSTS_SECONDS=3600` to `.env` and restarting. Browsers can't be told to
forget HSTS, so start small and only raise it to a year (`31536000`) when you
are sure.

`DJANGO_BEHIND_PROXY=true` makes the app believe whatever the proxy says about
http or https. That is only safe because Caddy is the only way in, which the
Caddy file arranges by removing the app's published port.

If a proxy already runs on the server, don't use the Caddy file. Its Caddy can't
start while something else holds ports 80 and 443. The next section covers that.

### If a proxy already runs on the server

Many servers already have one reverse proxy in Docker that fronts every site,
such as a shared Caddy, Traefik or Nginx Proxy Manager on ports 80 and 443. You
can tell: starting `docker-compose.caddy.yml` fails with "Bind for 0.0.0.0:80
failed: port is already allocated". Don't add a second proxy. Join the one you
have. `docker-compose.proxy.yml` puts the app on that proxy's Docker network, so
the proxy reaches it by name at `http://mediaflow-web:8000`, and it publishes no
port on the host. That also means the app can't clash with whatever else uses
port 8000 there.

1. Find the Docker network the proxy is on. Replace `caddy` with the name of
   your proxy's container, which `sudo docker ps` shows:

   ```bash
   sudo docker inspect caddy --format '{{range $name, $net := .NetworkSettings.Networks}}{{$name}}{{"\n"}}{{end}}'
   ```

   If it lists several, use the one it shares with your other sites. Run the
   same command on another site's container to compare.

2. In `.env`, use these in place of the Caddy lines, and keep the rest of the
   Django lines from the section above:

   ```text
   COMPOSE_FILE=docker-compose.yml:docker-compose.proxy.yml
   PROXY_NETWORK=the-network-name-from-step-1
   ```

3. Start the app on its own:

   ```bash
   sudo docker compose up -d --build
   ```

4. Add the site to the proxy. For a Caddy proxy, add this block to its
   Caddyfile. The Caddyfile's location is the "Source" that this prints:

   ```bash
   sudo docker inspect caddy --format '{{range .Mounts}}{{.Source}} -> {{.Destination}}{{"\n"}}{{end}}'
   ```

   ```text
   mediaflow.jessemcnary.com {
   	reverse_proxy mediaflow-web:8000
   }
   ```

   Then make Caddy reload, using the "Destination" path of the Caddyfile:

   ```bash
   sudo docker exec caddy caddy reload --config /etc/caddy/Caddyfile
   ```

   For Nginx Proxy Manager, add a Proxy Host for the domain with scheme `http`,
   forward host `mediaflow-web`, port `8000`, then request a certificate on its
   SSL tab and switch on Force SSL. Traefik needs the equivalent router and
   service pointing at `mediaflow-web:8000`.

The proxy must pass on the `Host` header and set `X-Forwarded-Proto: https`, which
Caddy and Nginx Proxy Manager both do. Without that the site redirects in a loop.
If you reach the proxy through Cloudflare, the DNS record and SSL mode advice
above still applies.

### Without touching the proxy: a Cloudflare Tunnel

If you would rather not change the proxy already on the server, or can't open
ports 80 and 443, use a tunnel. A `cloudflared` container on the server makes an
outgoing connection to Cloudflare, and Cloudflare sends your domain's traffic
down it. Cloudflare gives visitors the HTTPS certificate, so there is nothing to
forward on the router, no port to publish and no change to any other site. It
needs the domain to be on Cloudflare, which `jessemcnary.com` is.

1. In the Cloudflare dashboard, find **Tunnels**. It sits under Zero Trust, then
   Networks, or under Networking, depending on which layout you have. Create a
   tunnel of type **Cloudflared**, name it `mediaflow`, and copy the **token** it
   shows, which is a long string. Cloudflare may ask you to pick the Free plan
   first, and sometimes asks for a payment method that is not charged.
2. On the tunnel's **Public Hostname** step, add a hostname with subdomain
   `mediaflow`, domain `jessemcnary.com`, service type `HTTP` and URL
   `web:8000`. `web` is the app's name inside the compose network, which is how
   the tunnel container finds it. Cloudflare creates the DNS record for you. If
   an `A` record named `mediaflow` already exists, delete it first or Cloudflare
   will refuse.
3. Put these in the server's `.env`, and take out any other `COMPOSE_FILE`,
   `PROXY_NETWORK` or `MEDIAFLOW_DOMAIN` lines:

   ```text
   COMPOSE_FILE=docker-compose.yml:docker-compose.tunnel.yml
   TUNNEL_TOKEN="paste-the-token-here"
   DJANGO_ALLOWED_HOSTS="mediaflow.jessemcnary.com"
   DJANGO_CSRF_TRUSTED_ORIGINS="https://mediaflow.jessemcnary.com"
   DJANGO_BEHIND_PROXY=true
   DJANGO_SSL_REDIRECT=true
   ```

4. Start it, and watch the tunnel connect:

   ```bash
   sudo docker compose up -d --build
   ```

   ```bash
   sudo docker compose logs -f cloudflared
   ```

   Look for a line saying a tunnel connection was registered. The tunnel also
   shows as healthy in the dashboard. Then open `https://mediaflow.jessemcnary.com`.

The token is a secret. Anyone who has it can run your tunnel, so keep it in
`.env` and never commit it. If it leaks, delete the tunnel in Cloudflare and make
a new one. `DJANGO_BEHIND_PROXY=true` is safe here because no port is published,
so the tunnel is the only thing that can reach the app.

Things to know:

- **Email.** Unless `.env` sets up SMTP, confirmation emails are printed in
  `docker compose logs web`, so that is where to find a link.
- **Cookies.** Debug is off in the container, so cookies are marked Secure.
  Chrome and Firefox accept that on `http://localhost`. Safari doesn't, so sign
  in from Chrome or Firefox, or put HTTPS in front.
- **Port.** If 8000 is taken, set `HOST_PORT=8080` in `.env`.
- **One container.** SQLite is a single file, so run one copy of the container,
  not several sharing it.

To deploy the image to a host, give it the same variables as in Deploying
below: `DJANGO_SECRET_KEY`, `DJANGO_ALLOWED_HOSTS`, and, when the host ends
HTTPS for you, `DJANGO_BEHIND_PROXY=true` and `DJANGO_SSL_REDIRECT=true`. Attach
a persistent disk at `/data`. The container listens on `$PORT` (8000 if unset),
`GUNICORN_WORKERS` sets how many workers it starts (3 by default), and it
carries its own health check against `/healthz/`.

## Deploying

Running locally needs no setup: with nothing configured, debug is on and the
app uses development values. Deploying means one setting, `DJANGO_DEBUG=false`.
From then on the app is strict. It refuses to start unless `DJANGO_SECRET_KEY`
and `DJANGO_ALLOWED_HOSTS` are set, it won't accept a `django-insecure-` key,
and a mistyped value (`DJANGO_DEBUG=flase`) is an error rather than a guess.
It also changes these:

- Cookies are marked secure and plain http is redirected to https.
- Static files are served by WhiteNoise under hashed names, so a browser can
  never keep an old copy of the CSS after a deploy.
- Passwords must pass Django's four standard rules. They are off in development
  so throwaway accounts can use anything.
- Errors are logged to the console, and 404 and 500 pages are our own.

Settings come from the environment or from `.env` (see `.env.example`). A
variable set in the environment wins over the same one in the file.

| Variable | Default | What it does |
| --- | --- | --- |
| `DJANGO_DEBUG` | `true` | Set `false` in production. |
| `DJANGO_SECRET_KEY` | a dev key | Required when debug is off. |
| `DJANGO_ALLOWED_HOSTS` | none | Comma separated host names. Required when debug is off. |
| `DJANGO_CSRF_TRUSTED_ORIGINS` | none | Full origins such as `https://mediaflow.example.com`. Needed if form posts fail with a CSRF error behind a proxy. |
| `DJANGO_SECURE_COOKIES` | `true` | Mark the sign-in and form cookies Secure. Browsers drop those from plain http, except on localhost, so set `false` for a test server reached over http, and back to `true` once HTTPS is in front. |
| `DJANGO_SSL_REDIRECT` | `true` | Redirect http to https when debug is off. `false` lets you try production settings over plain http. |
| `DJANGO_BEHIND_PROXY` | `false` | Set `true` when a proxy or host ends HTTPS and sets `X-Forwarded-Proto`. Most hosts do, and without it the site redirects forever. Leave it off otherwise, because anyone can send that header. |
| `DJANGO_HSTS_SECONDS` | `0` | Off until you have seen HTTPS working, because browsers can't be told to forget it. Try `3600`, then `31536000`. |
| `DATABASE_PATH` | `db.sqlite3` | Where the SQLite file lives. On a host whose disk is wiped on each deploy, point this at a persistent volume. |
| `DJANGO_STATIC_ROOT` | `staticfiles/` | Where `collectstatic` puts the files. |
| `DJANGO_LOG_LEVEL` | `INFO` | `WARNING` is quieter. |
| `DJANGO_ENV_FILE` | `.env` | Read a different settings file. |
| `EMAIL_*` | console | See Email above. Production needs real mail. |

Make a secret key with:

```bash
python -c "import secrets; print(secrets.token_urlsafe(50))"
```

That one only uses letters, digits, `-` and `_`. Django's own generator can
produce `$` and `#`, which Docker reads as variable and comment syntax when the
key sits in a `.env` file.

With the variables set, a first deploy is:

```bash
pip install -r requirements.txt
python manage.py migrate
python manage.py collectstatic --noinput
gunicorn mediaflow.wsgi
```

`gunicorn` is a Linux server and isn't installed on Windows. `/healthz/` answers
`{"status": "ok"}` when the database responds and a 503 when it doesn't, over
plain http too, so a host's health check can use it.

Without working email nobody can confirm an account and every new sign-up is
stuck on the "check your email" page. `python manage.py check --deploy` warns
about that along with anything else risky, and CI runs it on every push.

### Checks on GitHub

`.github/workflows/checks.yml` runs on every push and pull request:

1. the migrations match the models (`makemigrations --check`)
2. the whole test suite
3. production settings load and pass `check --deploy` with warnings treated as
   failures
4. the committed `static/css/app.css` matches what `npm run build:css` makes

If the last one fails, you used a Tailwind class in a template and didn't
rebuild. Run `npm run build:css` and commit the result.

## Files

```
planner/
  models.py     catalog, backlog, schedule, suggestions, reviews
  services.py   free time detection + suggestion scoring
  views.py
  forms.py      includes the mavs.uta.edu signup check
  verification.py  signed email links, sending, resend limit
  middleware.py    holds unverified users on the confirm page
  tests.py      341 tests, mostly on the scheduling
  management/commands/seed_catalog.py
  management/commands/reset_accounts.py
  management/commands/import_catalog.py
  catalog_import.py   the Open Library, TMDB and RAWG importers
Dockerfile, docker-compose.yml   the container, see Running in Docker
docker-compose.caddy.yml, Caddyfile   optional HTTPS in front of it, see HTTPS on a real domain
docker-compose.proxy.yml   join a proxy that is already running instead, see If a proxy already runs on the server
docker-compose.tunnel.yml   reach it through a Cloudflare Tunnel instead, see Without touching the proxy
docker/healthcheck.py   what the container's health check runs
mediaflow/settings.py   everything configurable, read from the environment
mediaflow/env.py   reads .env and parses the variables
.github/workflows/checks.yml   the checks that run on every push
.env.example   copy to .env for real email settings
templates/
static/js/reminders.js   polls for upcoming sessions and shows notifications
static/js/stats.js       tooltips for the charts on the Progress page
static/src/app.css   Tailwind source (edit this)
static/css/app.css   built output (don't edit)
```

No JS framework. Running the app only needs Django. Tailwind is a dev-only
dependency for rebuilding the CSS.
