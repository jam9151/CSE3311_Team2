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

Then go to `http://127.0.0.1:8000/`.

`seed_catalog` loads 36 titles into the catalog. The `--demo` flag also makes a
test account with a full class/work schedule already filled in, which saves
having to enter one every time you want to try the scheduler:

- user `demo`, password `mediaflow123`

Signing up for real needs a `@mavs.uta.edu` address. Admin is at `/admin/`
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
| User accounts | done, `@mavs.uta.edu` only |
| Media search | done, filter by type / genre / length |
| Media details | done, shows whether it fits your free time today |
| Visual calendar | done, month grid + day panel |
| Weekly schedule | done, recurring blocks and one-off commitments, both editable |
| Priority system | done, 1-5, editable from the backlog list |
| Free time detection | done |
| Accept/reject suggestions | done, rejections feed back into the scoring |
| Review manager | done |
| Notifications | partly done, reminders while a tab is open, no background push yet |

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

## Files

```
planner/
  models.py     catalog, backlog, schedule, suggestions, reviews
  services.py   free time detection + suggestion scoring
  views.py
  forms.py      includes the mavs.uta.edu signup check
  tests.py      52 tests, mostly on the scheduling
  management/commands/seed_catalog.py
templates/
static/js/reminders.js   polls for upcoming sessions and shows notifications
static/src/app.css   Tailwind source (edit this)
static/css/app.css   built output (don't edit)
```

No JS framework. Running the app only needs Django. Tailwind is a dev-only
dependency for rebuilding the CSS.
