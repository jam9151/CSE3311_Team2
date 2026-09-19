# MediaFlow — Backlog Manager

CSE 3311 Team 2. A Django web app that keeps films, TV, books, and games in one
backlog, reads the gaps in your week, and tells you what actually fits the time
you have tonight.

The competitors each cover one medium and none of them read your calendar.
Letterboxd shows a runtime but never uses it to plan anything. Goodreads can
tell you that you are behind but not when you have time to read. MediaFlow
takes your schedule as input and answers the actual question: *what should I
start tonight?*

## Quick start

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python manage.py migrate
python manage.py seed_catalog --demo
python manage.py runserver
```

Open `http://127.0.0.1:8000/`.

`seed_catalog` loads 36 catalog titles. The `--demo` flag also creates a demo
student with a full class and work schedule, so the scheduler has something to
reason about immediately:

- username `demo`, password `mediaflow123`

Sign up for a real account with any `@mavs.uta.edu` address. The admin lives at
`/admin/` (`python manage.py createsuperuser`).

## What the prototype covers

Against the feature list in the inception deck:

| Feature | State |
| --- | --- |
| User accounts | Done, restricted to `@mavs.uta.edu` at signup |
| Media search | Done, with type / genre / length / sort filters |
| Media details | Done, including whether it fits your free time today |
| Visual calendar | Done, month grid with a per-day panel |
| Weekly schedule | Done, recurring blocks plus one-off commitments |
| Priority system | Done, 1–5, editable inline from the backlog |
| Free time detection | Done, the core of `services.py` |
| Accept / reject suggestions | Done, rejections feed back into scoring |
| Media review manager | Done, rate and write up anything you finish |
| Push notifications | Not built — genuinely a stretch item, needs service workers |

## How the scheduling works

Two steps, deliberately separate, both in `planner/services.py`:

**1. Free time detection.** You tell the app when you are *busy*, not when you
are free. `free_intervals()` walks your waking window, subtracts recurring
weekly blocks, dated commitments, and anything you have already accepted, and
returns the gaps that are left. Gaps under 20 minutes are dropped.

**2. Suggestion scoring.** For each gap, `suggest_for_day()` scores every
backlog item and proposes the best fit. The scoring weighs priority, whether
you have already started something, how well the item uses the gap, and whether
you can finish it in one sitting. Recent rejections push an item down for two
weeks.

The part that makes the output feel right is in `SESSION_RULES`
(`planner/models.py`). A film is all-or-nothing: if its runtime does not fit the
gap it is not a candidate at all. Books, TV, and games are taken in whole chunks
— a chapter, an episode, a session — and capped at one realistic sitting, so a
free Saturday does not get swallowed by a single twelve-hour reading block.

That is why a 2-hour morning gap gets a chapter while a long afternoon gets the
priority-5 film.

## Layout

```
planner/
  models.py     Catalog, backlog, schedule, suggestions, reviews
  services.py   Free time detection and the suggestion engine
  views.py      Page and action handlers
  forms.py      Including the @mavs.uta.edu signup rule
  tests.py      27 tests, heaviest on the scheduling logic
  management/commands/seed_catalog.py
templates/      Server-rendered, no build step
static/css/     Plain CSS, no framework
```

No JavaScript framework and no build step, which keeps the dependency-
deprecation risk from the inception deck close to zero: the only requirement is
Django.

## Tests

```powershell
python manage.py test
```
