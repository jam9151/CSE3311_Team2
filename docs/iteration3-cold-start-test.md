# Iteration 3 cold-start user test

The Iteration 2 write-up committed to running the classmate test again as a
cold start: a tester who has never seen MediaFlow signs up, confirms their
email, sets up a week, accepts a suggestion and marks it done, with nobody
helping. This is the script for that session, the sheet to fill in while it
runs, and where the results go.

The same path is also automated in `ColdStartFlowTests` in `planner/tests.py`,
which runs on every push. The automated test proves the path works. The live
test finds out whether a person can find it.

## Before the session

- A deployed copy, or `python manage.py runserver` on the facilitator's laptop
  with the tester driving. Either way the catalog must be loaded
  (`seed_catalog` is enough) and email must reach a real inbox, because
  confirming the address is part of the test. With the console backend the
  facilitator reads the link out, and the "find the email" step is skipped and
  noted.
- The tester uses their own `@mavs.uta.edu` address and a throwaway password.
  Afterwards `python manage.py reset_accounts` removes the account.
- One facilitator, one note-taker. The facilitator says only the task, not how
  to do it. If the tester is stuck for a full minute, note it as a failure for
  that step, give the smallest hint that unblocks them, and carry on.
- Ask the tester to think aloud. Record the time per step and every place
  they hesitate, go to the wrong page, or say "I'd expect this to...".

## The tasks

Read each task aloud as written. Do not name pages or buttons.

| # | Task as read to the tester | Done when | Watch for |
| --- | --- | --- | --- |
| 1 | "Make yourself an account." | They are on the "Check your email" page | Do they read the UTA-only note before typing a non-UTA address? |
| 2 | "Get into the app." | The schedule page opens from the emailed link | Time to find the email. Spam folder? Did they try to sign in instead of opening the link? |
| 3 | "You're up at 10 and in bed around 2 in the morning. Tell the app that." | Waking hours saved as 10:00 to 02:00 | Do they trust that 2:00 AM is accepted? Do they read the help line under the field? |
| 4 | "Put your classes for this week in." | At least one recurring block saved | Do they use the day-by-day form or look for a calendar? Does the free time per day make sense to them? |
| 5 | "Find something you've been meaning to watch, read or play and add it." | One backlog item exists | Discover versus Add item. Do they understand the type filter? |
| 6 | "Ask the app what to do tonight." | A suggestion is on screen | Which button do they reach for? Does the reason sentence make sense? Does "P3 · Normal" read as a priority? |
| 7 | "Put that on your calendar." | The suggestion is accepted | Add to calendar versus Not today. Do they notice it moved to Planned? |
| 8 | "Pretend you did it. Tell the app." | The session is marked done | Where do they look: Today, the calendar, or the backlog? |
| 9 | "Say what you thought of it." | A review is saved | Do they answer "worth the time?" without prompting? Do they notice the sharing checkbox? |
| 10 | "You've forgotten your password. Get back in." | Signed in again with a new password | Do they find the link on the sign-in page? Does the email arrive? |

The getting-started checklist on Today should be ticking off as they go. Note
whether they ever look at it, and whether it helps or gets in the way.

## After the session

Ask, and write down the answers in their words:

1. What did you expect to happen that didn't?
2. What would you use this for in a normal week, if anything?
3. Which page would you go to first tomorrow?
4. One thing to change.

Then the five quick ratings, 1 (disagree) to 5 (agree):

| Statement | Rating |
| --- | --- |
| I could set it up without help. | |
| The suggestion fitted the time I actually had. | |
| I understood what "Not today" would do. | |
| I knew where to go to mark something done. | |
| I would come back to it. | |

## The results sheet

One row per tester. Time in seconds; a dash where a hint was needed.

| Tester | T1 | T2 | T3 | T4 | T5 | T6 | T7 | T8 | T9 | T10 | Hints | Ratings (5) | Biggest problem |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| | | | | | | | | | | | | | |

Targets, set from the Iteration 1 sessions: every tester finishes tasks 1 to 8
with at most one hint, no single task over three minutes, and the five ratings
average 4 or better.

## What to do with it

- A problem two or more testers hit is a bug, not a preference. It gets fixed
  in Iteration 3 and the fix gets a test.
- A problem one tester hits goes in the Iteration 3 write-up under "observed
  once" with the quote, and is decided on there.
- Anything about wording is changed on the spot if the fix is a sentence.

The results sheet and the answers go in the Iteration 3 write-up next to the
automated test count, the same way the Iteration 2 write-up reported the
earlier sessions.
