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
from free sources, a few hundred at a time:

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
python -c "from django.core.management.utils import get_random_secret_key as k; print(k())"
```

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
  tests.py      291 tests, mostly on the scheduling
  management/commands/seed_catalog.py
  management/commands/reset_accounts.py
  management/commands/import_catalog.py
  catalog_import.py   the Open Library, TMDB and RAWG importers
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
