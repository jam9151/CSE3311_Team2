# MediaFlow: Iteration 4 (final) report

CSE 3311 Team 2. Branch `iteration-4`, 16 November 2026.

This is the last iteration. It closes the items the team listed after
Iteration 3, two of them from our own use of the app (the Today page had too
many empty panels, and the calendar did not show commitments), the rest from
the known-limitations list in the technical documentation, and then a second
round of features chosen for how much they improve the core loop: moving and
cancelling sittings, importing a class schedule, episode tracking, a settings
page, installing as an app, and bringing in lists people already keep on
Letterboxd and Goodreads. A third, shorter round added a mobile and
accessibility pass, a softer "Later" answer, a time-of-day preference in the
scoring, a semester view, series continuation, watching together with a
classmate, and a read-only API. The write-up follows the shape of the
Iteration 2 and 3 documents: what changed in the specification and design,
then testing.

## 4. Specification & Design

### 4.1 What changed since Iteration 3

| Area | Iteration 3 | Iteration 4 |
| --- | --- | --- |
| Today | Six fixed panels, several often empty | Every panel can be hidden and brought back; a "Did these happen?" panel for sittings whose time passed; a weekly goal bar |
| Calendar | Planned sittings only in the grid | Commitments (amber) and sittings (green) in each day, busy time per day, click a day to plan something, add a commitment, skip a class or take the day off |
| Sittings | Accept, reject, mark done | Also "Later" (back tomorrow, no penalty), move (day, time, length) and remove |
| Scheduler | One suggestion per free gap; a sitting never crosses midnight; only the user's priority counted | A long gap is shared out with breaks; on an overnight schedule the evening gap can run past midnight; other students' ratings and the user's own time-of-day habits nudge the score |
| Social | Shared reviews | Plus Watch together: a classmate, a title, and a stretch the planner finds free for both |
| Term | Week by week only | A semester view of busy hours per week, busiest and lightest weeks |
| Series | Nothing | Next in the series on the catalog page and on Reviews, from TMDB collections |
| Phone and keyboard | Desktop-first | Mobile pass on every page; a short two-row nav on phones; skip link, focus ring, described controls, column headers, real buttons behind auto-submitting selects |
| Appearance | Dark only | Light and dark palettes, following the device by default, with a System / Light / Dark picker in the footer and on Settings |
| API | None | Read-only JSON with bearer tokens from Settings |
| Backlog | Minutes only | Episode and chapter positions, a per-item episode length, "episodes already finished" on the form; import from Letterboxd or Goodreads |
| Schedule | Typed by hand | Import an .ics calendar; exceptions per date |
| Reviews | Signed with the username | Display name, or anonymous per review |
| Progress | Only sittings marked done were charted | Logged time with a date is charted; this week against a goal |
| Reminders | Only while a tab is open | Background push once VAPID keys are set |
| Catalog | Imports overwrote hand edits; seed crashed on a duplicate | "Keep my edits" honoured; look a missing title up on the spot |
| Account | Settings scattered over three pages | One Settings page, data export, account deletion, installable as an app |
| Database | SQLite only | `DATABASE_URL` switches to PostgreSQL; a compose overlay runs it |
| Testing | Django test client only | Plus five Playwright browser tests, run in CI |
| Operations | Nothing written down | Backup script and a runbook |

### 4.2 Inputs

New inputs this iteration, all from signed-in students unless noted:

| Input | Where | Validation |
| --- | --- | --- |
| Which Today panels to show | Hide link on a panel, the customizer, or Settings | Unknown keys ignored; stored as a comma list in page order |
| A sitting placed or moved by hand | Calendar day panel, Move on a planned card | Item must be the student's and unfinished; 10 to 720 minutes; must not run past midnight or overlap busy time (its own slot excluded when moving) |
| A commitment started from a day | Calendar day panel | Same form as before, prefilled with the first free hour |
| A skipped block or a day off | Calendar day panel | The date must fall on the block's weekday; skipping twice is harmless |
| Episode length and episodes finished | Backlog edit form | 5 to 600 minutes; finished episodes convert to minutes capped at the estimate |
| Weekly goal | My week or Settings | 0 to 10,080 minutes; 0 is no goal |
| Display name and anonymous flag | Settings, Reviews, the review form | Up to 40 characters, trimmed |
| Time logged by hand | Backlog, "Log time" | 1 to 1440 minutes, date not in the future |
| A calendar file | My week, "Import a calendar" | Must contain `BEGIN:VCALENDAR`; up to 5 MB; weekly and daily repeats, one-off events in the next year |
| A Letterboxd or Goodreads CSV | Backlog, "Import" | Recognised by its columns; up to 5 MB; rows without a title skipped |
| A title to look up | Discover, "Look it up" | At least two characters; only sources with a key are asked |
| Password, to delete the account | Settings | Must match, plus a confirmation checkbox |
| Push subscription | Browser, after notifications are allowed | JSON with an `https` endpoint and both keys |
| A classmate and a slot, for Watch together | Catalog page, "Invite someone" | Username must exist, be confirmed and not be yours; the slot must still be free for both when sent |
| An API token name | Settings | Up to 60 characters; the token is shown once |
| Weeks to show on the semester view | Query string | 4 to 26, else 16 |
| `DATABASE_URL`, `VAPID_*`, `POSTGRES_PASSWORD` | Environment | Deployment only; bad values stop start-up |

### 4.3 Outputs

- Today with only the chosen panels, the weekly goal bar, and "Did these
  happen?" for sittings whose time passed unmarked.
- Calendar cells with up to three entries, "+N more", and a busy label such
  as "4h busy"; skipped blocks struck through in the day panel.
- A planned or moved sitting that behaves exactly like an accepted
  suggestion; a removed one frees its time.
- Suggestions that say which episodes they cover ("episodes 5–7 fit
  comfortably") and cards that show the position ("Episode 5 of 24 next").
- On an overnight schedule, a late film offered from 22:30 to 01:00, marked
  "past midnight", stored on the date it starts.
- Shared reviews signed with the display name or "A student".
- Progress with this week against the goal, and logged time on its date.
- A push notification with title, body and a tag, which opens Today.
- Import summaries: blocks and commitments added, rows matched or added by
  title, and what was left out and why.
- A JSON export of everything the student put in.
- The semester view: a bar per week, the busiest week in amber, commitments
  listed under each week.
- Watch together: the stretches free for both people (never what either is
  busy with), the invite on the guest's Today, and one accepted sitting on
  each calendar.
- "Next in the series" cards with Add to backlog, and a `series` line on the
  catalog page.
- JSON from the four API paths, and a 401 with `WWW-Authenticate` without a
  valid token.

### 4.4 Data structures

Three migrations. Nothing existing was removed or renamed.

| Model | Change |
| --- | --- |
| `Profile` | `hidden_panels`, `display_name`, `weekly_goal_minutes`; waking-hour defaults are real `time` objects |
| `BacklogItem` | `unit_minutes`; derived `units_total`, `units_done`, `next_unit`, `progress_label`, `units_label(minutes)` |
| `Review` | `anonymous` |
| `TimeLog` (new) | user, backlog item, date, minutes, note |
| `BlockException` (new) | user, recurring block, date; unique per block and date |
| `CatalogItem` | `locked` ("Keep my edits") |
| `PushSubscription` (new) | user, endpoint (unique), keys, user agent, last sent, failures |
| `SentReminder` (new) | user and reminder key, unique together; pruned after two days |
| `Suggestion` | no new fields; an end time earlier than the start marks a sitting that crosses midnight |
| `BacklogItem` (round three) | `snoozed_until` for "Later" |
| `CatalogItem` (round three) | `series`, `series_order` |
| `WatchInvite` (new) | host, guest, title kept by name, date, start, end, minutes, message, status |
| `ApiToken` (new) | user, name, prefix, SHA-256 of the token, created and last used |

`DASHBOARD_PANELS` in `models.py` is the one list of Today's seven panels.
`Interval` (in `services.py`) gained `extension`, the free minutes past
midnight a sitting started in it may use, and `usable`, which the planner
spends while `minutes` stays what the pages add up.

### 4.5 Algorithms

**Gap splitting.** `suggest_for_day` keeps the free gaps in a pool sorted by
usable length and takes the largest. After the best item takes its sitting,
`_remainder_gap` subtracts the sitting and a 15-minute break; if 45 minutes
or more remain, the remainder goes back in the pool. A film that fills its
gap leaves nothing to split, and nothing is split after a sitting that
crossed midnight.

**Past midnight.** On an overnight schedule, the last gap of a date that runs
to 23:59 carries an extension: the free minutes at the start of the next
date, up to the first busy thing there or bedtime. Fitting and gap fill use
the gap plus its extension; the pages still count each date on its own, so
nothing is double-counted. A sitting that runs over is stored on the date
it starts with an end time earlier than its start; `busy_blocks` clips it
to 23:59 on that date and blocks midnight to its end on the next.

**Community ratings.** The average rating other students gave the same
catalog title, from shared reviews, adds half a priority step scaled by
`(average - 3) / 2`: a title rated 5 gets the whole bonus, one rated 1 loses
it. The user's own priority always outranks the crowd.

**Episode positions.** No extra storage: `units_total` is the estimate
divided by the unit length (the type's default, or the item's own
`unit_minutes`), `units_done` the minutes done divided the same way, and the
next episode follows. A sitting's reason names the episodes it covers.

**Busy time and exceptions.** `busy_blocks` leaves out any recurring block
with a `BlockException` on that date, so a skipped class is free time to the
planner, the calendar and the day panel alike. `month_grid` makes three
queries for the grid: sittings, commitments overlapping the 42 days (clipped
per date), and recurring blocks summed per weekday.

**Calendar import.** Lines are unfolded, each `VEVENT` read, and a weekly or
daily `RRULE` with interval one and no past `UNTIL` becomes one recurring
block per `BYDAY` weekday with the kind guessed from the summary. Other
events become commitments if they end after now and start within a year.
All-day events, other frequencies and anything already over are counted by
reason. Times honour `TZID` and `Z`.

**Watchlist import.** A Letterboxd file is known by its `Letterboxd URI`
column, a Goodreads one by `Title`, `Author` and `Exclusive Shelf`. Each row
is matched to the catalog by title and type, preferring the same year, else
added by name with a default length. Shelves become statuses; a Goodreads
rating becomes a private review.

**On-demand lookup.** Each source has a `search` that goes through the same
record builder as the bulk import, so the same length and genre rules apply.
`lookup_title` asks every source with a key, saves what comes back, and
names any source that failed.

**Time of day.** The user's answers over the last sixty days are tallied per
media type and part of the day. With two or more answers, the score adds
half a priority step times `(for - against) / (for + against)` for the
gap's part of the day. "Later" counts for nothing, and the title is simply
not a candidate until the next day.

**Shared slots.** For each of the next fourteen days, both users' free gaps
(planned sittings counted as busy) are intersected, and the first six
stretches at least as long as the title are offered. Accepting creates an
accepted sitting for each person, reusing a backlog entry where one exists.

**Semester view.** Fixed busy time per week from this Monday: recurring
blocks minus exceptions, plus commitments clipped per date, in three
queries; planned sittings are left out so the page shows the shape of the
term rather than what was planned into it.

**Next in the series.** Rows sharing a series and type, ordered by
`series_order` or release date, with what comes before this one and what is
already on the backlog left out.

**Push, once.** `push_due_reminders` runs the same reminder logic as the
open-tab poller for every subscribed user, skips keys already sent, pushes
the rest to each browser, drops subscriptions the push service reports gone,
and records the key as sent whatever happened.

### 4.6 Use cases

| Use case | Main flow | Exceptions |
| --- | --- | --- |
| Hide a panel | Today → Hide → reloads without it | All hidden: a note links to the customizer |
| Plan something on a day | Calendar → day → + Plan something → item, time, minutes → save | Overlap or past midnight: refused with the reason |
| Move a sitting | Planned card → Move → change day, time or length → Save the move | Its own slot does not count as busy; overlaps elsewhere refused |
| Remove a sitting | Planned card → Remove | Only accepted sittings; pending ones use Not today |
| Answer "Did these happen?" | Today → Yes, mark done / Didn't happen | The panel disappears once nothing is waiting |
| Skip a class | Calendar → day → skip next to the block, or Day off | Wrong weekday refused; restore from the day panel or My week |
| Track episodes | Backlog → Edit → episode length and episodes finished → save | Finished episodes capped at the estimate |
| Set a weekly goal | My week or Settings → minutes → save | Over a week's worth refused |
| Import a schedule | My week → Import a calendar → .ics → Import | Not a calendar, or nothing usable: told why |
| Import a watchlist | Backlog → Import → CSV, want to / have finished → Import | Unknown format refused |
| Look a title up | Discover → search → Look it up | No sources set up: pointed at Add manually; a failing source named |
| Change settings | Settings → the relevant form → save | Each form validates on its own |
| Export or delete | Settings → Download, or Delete… → password and checkbox | Wrong password refused; deletion cascades and signs out |
| Install the app | Browser menu → Install / Add to Home Screen | — |
| Put it off | Suggestion card → Later | Pending suggestions only; back tomorrow |
| Watch together | Catalog page → Invite someone → username → Find a time → pick → Send | Unknown, unconfirmed or own username refused; no shared stretch: told so; stale slot refused |
| Answer an invite | Today → Accept / Decline; host can Withdraw | Only the guest answers, only the host withdraws |
| See the term | My week or Calendar → Semester | Empty schedule: pointed at My week and the import |
| Follow a series | Catalog page or Reviews → Next in the series → Add to backlog | Nothing when the series is unknown |
| Use the API | Settings → Make a token → `Authorization: Bearer` | Missing or wrong token: 401 |

### 4.7 Screen transitions

New routes, all under the existing navigation, with Settings added to the
bar in place of the bare Change password link:

| Route | View | From |
| --- | --- | --- |
| `POST /dashboard/panels/` | `dashboard_panels` | Today, Settings |
| `GET/POST /calendar/plan/?date=` and `?move=<pk>` | `plan_session` | Calendar day panel, planned cards |
| `POST /suggestions/<pk>/cancel/` | `respond_to_suggestion` | Planned cards, "Did these happen?" |
| `POST /schedule/block/<pk>/skip/`, `/schedule/day-off/`, `/schedule/skip/<pk>/delete/` | block exceptions | Calendar day panel, My week |
| `GET/POST /schedule/import/` | `import_schedule` | My week |
| `GET/POST /backlog/import/` | `import_backlog` | Backlog |
| `GET/POST /backlog/<pk>/log/` | `log_time` | Backlog |
| `POST /discover/lookup/` | `lookup_catalog` | Discover |
| `GET/POST /settings/`, `GET /settings/export/`, `GET/POST /settings/delete/` | settings views | Navigation bar |
| `POST /reviews/name/` | `set_display_name` | Reviews |
| `POST /suggestions/<pk>/later/` | `respond_to_suggestion` | Suggestion cards |
| `GET/POST /media/<pk>/watch-together/`, `POST /invites/<pk>/<action>/` | watch together | Catalog page, Today |
| `GET /semester/` | `semester` | My week, Calendar |
| `POST /settings/tokens/new/`, `/settings/tokens/<pk>/revoke/`, `GET /api/v1/…` | API | Settings, external clients |
| `GET /sw.js`, `/notifications/push/…` | push views | `reminders.js` |

Every form reached from the calendar returns to the day it came from through
a `next` parameter, accepted only when it points at this site.

### 4.8 Consistency rules

- A sitting placed or moved by hand is an accepted `Suggestion` with a reason
  that says so. Nothing else in the app distinguishes it.
- A skipped block is skipped everywhere: planner, calendar, day panel, My
  week, reminders.
- Episode positions, the "episodes 5–7" in a reason and the sittings
  proposed all come from the same unit length.
- A reminder key is the same string for the poller, the push sender and the
  browser, so no reminder is shown twice by any route.
- `DATABASE_URL` wins over `DATABASE_PATH`; an unknown scheme stops start-up.
- Imports never add what is already there: blocks and commitments are
  matched on their fields, backlog rows on the catalog item or the title.
- Push, PostgreSQL and the lookup sources are pure add-ons: with nothing
  configured, every page behaves as in Iteration 3.
- Watch together never shows one person the other's busy time, only the
  stretches free for both.
- The API is tokens only: a signed-in session gets a 401, so a page's
  cookies can never be replayed against it.
- Every control that acts on a named thing says which thing in its
  accessible name, and every keyboard-reachable element shows the same
  focus ring.
- Both palettes define the same variables, so a colour added to one must be
  added to the other; the test suite checks that both are present in the
  built stylesheet.

### 4.9 Known gaps

- A sitting placed by hand stays within one date; only the planner's own
  suggestions run past midnight.
- Episode positions are arithmetic, so a show with uneven episode lengths is
  approximate unless its unit length is set.
- Calendar import handles weekly and daily repeats only; every-other-week
  classes are skipped and counted.
- Watchlist import matches by title; same-titled films without a year take
  the first catalog row.
- The on-demand lookup runs inside the request and takes a few seconds.
- Push on iOS needs the site added to the home screen.
- The PostgreSQL overlay is configured and unit-tested but has not been run
  against a real server.
- Watch together is two people by exact username; there is no friends list
  or group invite.
- Series data exists only for films imported from TMDB unless entered by
  hand.
- The API is read-only.

## 6. Testing

### 6.1 Strategy

Unchanged in kind, larger in size: every behaviour above has unit tests
through Django's test client, pinned to fixed dates so results do not depend
on when the suite runs. New this iteration is a browser layer: five
Playwright tests drive a headless Chromium against a live server, covering
the flows where buttons, forms and redirects have to hang together in a way
the test client cannot see.

### 6.2 What the pipeline checks

The GitHub Actions workflow runs, on every push and pull request:

1. `makemigrations --check` (a model change without a migration fails).
2. The unit suite.
3. `pip install -r requirements-dev.txt`, `playwright install chromium`, and
   the browser tests on their own.
4. `check --deploy` under production settings, warnings counted as failures.
5. The Tailwind build, failing if the committed CSS is stale.
6. A Docker build and a health-check against the running container.

### 6.3 Coverage by feature

| Feature | Test classes | Tests |
| --- | --- | --- |
| Today panels, follow-up prompt | `DashboardPanelTests`, `FollowUpTests` | 13 |
| Calendar grid | `CalendarGridTests` | 7 |
| Plan, move, remove, commitment from a day | `PlanSessionTests`, `MoveAndCancelTests`, `CommitmentFromCalendarTests` | 20 |
| Block exceptions | `BlockExceptionTests` | 9 |
| Gap splitting, midnight, community ratings | `GapSplittingTests`, `MidnightSittingTests`, `CommunityScoringTests`, plus four updated scheduler tests | 22 |
| Episode tracking, weekly goal | `UnitTrackingTests`, `WeeklyGoalTests` | 14 |
| Display name, anonymous reviews | `DisplayNameTests` | 7 |
| Logged time | `TimeLogTests` | 10 |
| Settings, export, delete, install | `SettingsPageTests`, `ExportAndDeleteTests`, `InstallableAppTests` | 16 |
| Calendar and watchlist import | `IcsReadTests`, `ScheduleImportViewTests`, `MediaCsvReadTests`, `MediaImportApplyTests` | 21 |
| On-demand lookup | `LookupTests`, `LookupViewTests` | 10 |
| Accessibility markup, theme and nav, Later, time of day | `AccessibilityTests`, `ThemeAndNavTests`, `SnoozeTests`, `TimeOfDayTests` | 21 |
| Semester view, series, watch together, API | `SemesterViewTests`, `SeriesTests`, `WatchTogetherTests`, `ApiTests` | 24 |
| Catalog lock and seed | `CatalogLockTests`, `SeedCatalogTests` | 9 |
| `DATABASE_URL` | `DatabaseUrlTests` | 6 |
| Push | `PushEndpointTests`, `PushSenderTests`, `PushCommandTests` | 25 |
| Deployment files, profile defaults | `DeploymentFilesTests`, `StaffWithoutProfileTests` and one template check | 7 |
| Browser | `BrowserFlowTests` (`tests_e2e.py`) | 5 |

The unit suite is 646 tests, up from 403 at the end of Iteration 3, and
passes. The five browser tests pass locally and run in CI.

### 6.4 Sample test cases

- *A free Saturday gets several sittings with breaks between.* Three books,
  a free day: sittings at 8:00, 9:30 and 11:00, 75 minutes each.
- *A film that only fits across midnight is offered.* Up by 10:00, in bed by
  02:00, busy until 22:30: a 150-minute film is offered 22:30 to 01:00 on the
  date it starts, and once accepted the next date's small hours are busy
  until 01:00.
- *A loved title beats an unrated equal but not a higher priority.* Two
  priority-3 books, one rated 5 by a classmate: the loved one wins; raise the
  other to priority 4 and it wins instead.
- *Skipping frees that date only.* A lecture skipped on one Tuesday leaves
  the day free; the next Tuesday still has it.
- *Moving changes day, time and length in place,* and a sitting can move
  within its own time because its slot does not count as busy.
- *Position is derived from minutes and unit length.* 180 minutes into a
  show of 45-minute episodes is "Episode 5 of 24 next"; with a 60-minute
  unit it is episode 4 of 18.
- *Weekly repeats become blocks per weekday.* A `BYDAY=MO,WE` lecture gives
  two blocks; an event with no `BYDAY` lands on its own weekday.
- *Catalog matches prefer the same year.* "Dune, 2021" matches the 2021 row
  when a 1984 row is also in the catalog.
- *Lookup asks each usable source and saves what comes back.* With fake
  answers from all three services, three catalog rows appear.
- *Deleting removes everything and signs out.* After the password and the
  checkbox, every table is empty for that user.
- *A due reminder is pushed once.* A sitting ten minutes away is pushed to
  one browser; a minute later nothing is sent again.
- *Shared slots intersect both free times.* Two people busy until 19:00,
  one also busy from 21:00: a 116-minute film gets 19:00 to 20:56, a
  150-minute one gets nothing that day.
- *Accepting puts the sitting on both calendars,* reusing the guest's
  existing backlog entry, and neither is free then any more.
- *Films after dinner win once the user has said so.* Two rejections of a
  film at 19:00 and two acceptances of a book at 19:00 flip the pick.
- *Weeks sum recurring time, exceptions and commitments.* A skipped shift
  and an exam week give 360, 120 and 540 minutes.
- *Next in the series is ordered and skips what you have.*
- *A session does not count for the API.* Signed in without a token: 401.
- Browser: *sign up, confirm by email, land on My week*; *suggest, accept
  from the calendar and mark done on Today*; *plan something from a calendar
  day*; *hide a Today panel and bring it back*; *log time shows up on
  Progress*.

### 6.5 Manual and user testing

Run through in the built-in browser on a throwaway local account: Today,
the calendar grid, Plan something, + Commitment, hiding a panel and the
customizer, the Log time form, the display-name box on Reviews, and the
Settings page. The mobile pass was done the same way at a 375-pixel
viewport on every page: the navigation became two short rows with the links
scrolling sideways, the calendar grid swapped its busy labels for dots, the
backlog action rows wrapped, and nothing overflowed. The light palette was
checked on Today and the calendar at both widths.

Not run: the classmate user test of the UI changes, and a live push
delivery, which needs HTTPS and a configured key pair. Both are listed in
`docs/iteration4-todo.txt` with the steps.

### 6.6 Known limitations of the testing

- The push sender is tested with a fake sender and a mocked `pywebpush`;
  no real push service was contacted.
- The on-demand lookup is tested against a fake fetcher; the live services
  were not called from the test suite.
- The PostgreSQL path is tested at the settings level only.
- Browser tests use one headless Chromium.
- Live email delivery still cannot be proven by tests.
