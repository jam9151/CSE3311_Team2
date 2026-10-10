import io
import json
import os
import re
import smtplib
import tempfile
import urllib.error
import urllib.parse
from datetime import date, datetime, time, timedelta
from email.message import Message
from pathlib import Path
from unittest import mock

from django.contrib.auth import get_user_model
from django.core import mail, signing
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from django.utils import timezone

from mediaflow.env import clean_smtp_password, load_env_file, parse_env_file

from .catalog_import import (
    BookSource,
    GameSource,
    HttpFetcher,
    ImportFailure,
    MovieSource,
    Record,
    Source,
    TvSource,
    best_book_label,
    clip,
    normalize_genre,
    one_decimal,
    parse_date,
    save_record,
    tidy_description,
)
from .forms import RegisterForm
from .models import BacklogItem, CatalogItem, Commitment, MediaType, Profile, RecurringBlock, Review, Suggestion
from .services import (
    free_intervals,
    humanize_minutes,
    _axis,
    month_grid,
    progress_stats,
    suggest_for_day,
    suggest_for_week,
    total_free_minutes,
    upcoming_reminders,
)
from .verification import TOKEN_MAX_AGE_DAYS

# A Tuesday. Most of these tests pin a fixed date because free_intervals()
# clips gaps that have already passed when the day is today, which would make
# the results depend on when you run the suite.
TEST_DAY = date(2026, 10, 6)


def make_user(username="sam", **profile_kwargs):
    user = get_user_model().objects.create_user(
        username=username, password="test-password", email=f"{username}@mavs.uta.edu"
    )
    # Verified by default so tests can reach the app. VerificationTests builds
    # unverified users by going through the registration form instead.
    Profile.objects.create(
        user=user, **{"day_start": time(8), "day_end": time(23), "email_verified": True, **profile_kwargs}
    )
    return user


class FreeTimeTests(TestCase):
    def setUp(self):
        self.user = make_user()

    def test_empty_day(self):
        gaps = free_intervals(self.user, TEST_DAY)
        self.assertEqual(len(gaps), 1)
        self.assertEqual((gaps[0].start, gaps[0].end), (time(8), time(23)))
        self.assertEqual(gaps[0].minutes, 15 * 60)

    def test_block_splits_day(self):
        RecurringBlock.objects.create(
            user=self.user, label="CSE 3311", weekday=TEST_DAY.weekday(),
            start_time=time(11), end_time=time(12, 20),
        )
        gaps = free_intervals(self.user, TEST_DAY)
        self.assertEqual([(g.start, g.end) for g in gaps], [(time(8), time(11)), (time(12, 20), time(23))])

    def test_overlapping_blocks(self):
        # 9-3 and 2-5 should come out as one busy stretch of 9-5, not two.
        RecurringBlock.objects.create(
            user=self.user, label="Work", weekday=TEST_DAY.weekday(), start_time=time(9), end_time=time(15)
        )
        RecurringBlock.objects.create(
            user=self.user, label="Meeting", weekday=TEST_DAY.weekday(), start_time=time(14), end_time=time(17)
        )
        gaps = free_intervals(self.user, TEST_DAY)
        self.assertEqual([(g.start, g.end) for g in gaps], [(time(8), time(9)), (time(17), time(23))])

    def test_dated_commitment(self):
        Commitment.objects.create(
            user=self.user, title="Group project",
            starts_at=timezone.make_aware(datetime(2026, 10, 6, 18, 0)),
            ends_at=timezone.make_aware(datetime(2026, 10, 6, 20, 0)),
        )
        gaps = free_intervals(self.user, TEST_DAY)
        self.assertEqual([(g.start, g.end) for g in gaps], [(time(8), time(18)), (time(20), time(23))])

    def test_short_gap_dropped(self):
        RecurringBlock.objects.create(
            user=self.user, label="Class", weekday=TEST_DAY.weekday(), start_time=time(8, 10), end_time=time(23)
        )
        # Ten minutes at the start of the day is not a usable gap.
        self.assertEqual(free_intervals(self.user, TEST_DAY), [])

    def test_waking_hours_limit_search(self):
        self.user.profile.day_start = time(10)
        self.user.profile.day_end = time(20)
        self.user.profile.save()
        self.assertEqual(total_free_minutes(self.user, TEST_DAY), 10 * 60)


class SuggestionTests(TestCase):
    def setUp(self):
        self.user = make_user()

    def leave_gap(self, start=time(19), end=time(23)):
        """Block out everything except start-end, so there's one known gap."""
        RecurringBlock.objects.create(
            user=self.user, label="Busy", weekday=TEST_DAY.weekday(), start_time=time(8), end_time=start
        )
        self.user.profile.day_end = end
        self.user.profile.save()

    def add(self, title, media_type, minutes, priority=3, **kwargs):
        return BacklogItem.objects.create(
            user=self.user, title=title, media_type=media_type,
            estimated_minutes=minutes, priority=priority, **kwargs
        )

    def test_movie_needs_whole_runtime(self):
        self.leave_gap(time(21), time(23))  # 2 hours
        self.add("Long film", MediaType.MOVIE, 200, priority=5)
        self.assertEqual(suggest_for_day(self.user, TEST_DAY), [])

    def test_book_fits_where_movie_does_not(self):
        self.leave_gap(time(21), time(23))
        self.add("Long film", MediaType.MOVIE, 200, priority=5)
        book = self.add("A book", MediaType.BOOK, 600, priority=2)

        suggestions = suggest_for_day(self.user, TEST_DAY)
        self.assertEqual(len(suggestions), 1)
        self.assertEqual(suggestions[0].backlog_item, book)
        # 90 min cap, rounded down to three 25-min chapters. Not the full 2h.
        self.assertEqual(suggestions[0].minutes, 75)

    def test_one_item_cannot_eat_whole_day(self):
        self.leave_gap(time(9), time(21))  # 12 hours free
        self.add("Doorstopper", MediaType.BOOK, 3000, priority=5)
        self.assertEqual(suggest_for_day(self.user, TEST_DAY)[0].minutes, 75)

    def test_almost_done_item_runs_to_the_end(self):
        self.leave_gap()
        self.add("Almost done", MediaType.BOOK, 600, minutes_completed=560)
        suggestion = suggest_for_day(self.user, TEST_DAY)[0]
        self.assertEqual(suggestion.minutes, 40)
        self.assertIn("finish", suggestion.reason)

    def test_priority_breaks_the_tie(self):
        self.leave_gap()
        self.add("Low", MediaType.BOOK, 600, priority=1)
        high = self.add("High", MediaType.BOOK, 600, priority=5)
        self.assertEqual(suggest_for_day(self.user, TEST_DAY)[0].backlog_item, high)

    def test_rejection_demotes_item(self):
        self.leave_gap()
        rejected = self.add("Turned down", MediaType.BOOK, 600, priority=5)
        other = self.add("Other", MediaType.BOOK, 600, priority=4)

        # P5 wins on a clean slate.
        self.assertEqual(suggest_for_day(self.user, TEST_DAY)[0].backlog_item, rejected)

        Suggestion.objects.filter(backlog_item=rejected).update(status=Suggestion.Status.REJECTED)
        Suggestion.objects.filter(backlog_item=other).delete()

        # After a no, the lower-priority item should come out on top.
        self.assertEqual(suggest_for_day(self.user, TEST_DAY)[0].backlog_item, other)

    def test_completed_items_skipped(self):
        self.leave_gap()
        self.add(
            "Done", MediaType.BOOK, 600, priority=5,
            minutes_completed=600, status=BacklogItem.Status.COMPLETED,
        )
        self.assertEqual(suggest_for_day(self.user, TEST_DAY), [])

    def test_accepted_time_is_no_longer_free(self):
        self.leave_gap()  # 19:00-23:00
        item = self.add("A book", MediaType.BOOK, 600)
        Suggestion.objects.create(
            user=self.user, backlog_item=item, date=TEST_DAY, start_time=time(19), end_time=time(21),
            minutes=120, status=Suggestion.Status.ACCEPTED,
        )
        gaps = free_intervals(self.user, TEST_DAY)
        self.assertEqual([(g.start, g.end) for g in gaps], [(time(21), time(23))])

    def test_no_duplicate_items_across_gaps(self):
        # Two evening gaps split by dinner.
        RecurringBlock.objects.create(
            user=self.user, label="Busy", weekday=TEST_DAY.weekday(), start_time=time(8), end_time=time(17)
        )
        Commitment.objects.create(
            user=self.user, title="Dinner",
            starts_at=timezone.make_aware(datetime(2026, 10, 6, 19, 0)),
            ends_at=timezone.make_aware(datetime(2026, 10, 6, 20, 0)),
        )
        for i in range(3):
            self.add(f"Book {i}", MediaType.BOOK, 600)

        suggestions = suggest_for_day(self.user, TEST_DAY)
        self.assertEqual(len(suggestions), 2)
        self.assertEqual(len({s.backlog_item_id for s in suggestions}), 2)


class HelperTests(TestCase):
    def test_humanize_minutes(self):
        self.assertEqual(humanize_minutes(45), "45 min")
        self.assertEqual(humanize_minutes(120), "2h")
        self.assertEqual(humanize_minutes(200), "3h 20m")

    def test_month_grid_sunday_first(self):
        weeks = month_grid(make_user("grid"), 2026, 10)
        self.assertTrue(all(len(week) == 7 for week in weeks))
        # Oct 1 2026 is a Thursday, so the grid opens on Sunday Sept 27.
        self.assertEqual(weeks[0][0]["date"], date(2026, 9, 27))
        self.assertFalse(weeks[0][0]["in_month"])
        self.assertTrue(weeks[0][4]["in_month"])


class RegisterTests(TestCase):
    form_data = {"password1": "sturdy-passphrase-1", "password2": "sturdy-passphrase-1"}

    def test_rejects_non_uta_email(self):
        form = RegisterForm(data={"username": "outsider", "email": "me@gmail.com", **self.form_data})
        self.assertFalse(form.is_valid())
        self.assertIn("email", form.errors)

    def test_accepts_mavs_email(self):
        form = RegisterForm(data={"username": "student", "email": "student@mavs.uta.edu", **self.form_data})
        self.assertTrue(form.is_valid(), form.errors)
        user = form.save()
        self.assertTrue(Profile.objects.filter(user=user).exists())


class PageTests(TestCase):
    def setUp(self):
        self.user = make_user()

    def test_home_is_public(self):
        self.assertContains(self.client.get("/"), "deciding what to start")

    def test_dashboard_requires_login(self):
        self.assertRedirects(self.client.get("/dashboard/"), "/accounts/login/?next=/dashboard/")

    def test_pages_load(self):
        self.client.force_login(self.user)
        for url in ["/dashboard/", "/calendar/", "/backlog/", "/discover/", "/schedule/", "/reviews/"]:
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 200)

    def test_discover_filters(self):
        CatalogItem.objects.create(title="Short game", media_type=MediaType.GAME, typical_minutes=180)
        CatalogItem.objects.create(title="Epic game", media_type=MediaType.GAME, typical_minutes=3600)
        CatalogItem.objects.create(title="A film", media_type=MediaType.MOVIE, typical_minutes=120)

        self.client.force_login(self.user)
        response = self.client.get("/discover/", {"media_type": "game", "max_minutes": 200})
        self.assertContains(response, "Short game")
        self.assertNotContains(response, "Epic game")
        self.assertNotContains(response, "A film")

    def test_add_from_catalog(self):
        catalog_item = CatalogItem.objects.create(
            title="Portal 2", media_type=MediaType.GAME, typical_minutes=500
        )
        self.client.force_login(self.user)
        self.client.post(f"/media/{catalog_item.pk}/add/")

        entry = self.user.backlog_items.get()
        self.assertEqual(entry.title, "Portal 2")
        self.assertEqual(entry.estimated_minutes, 500)

    def test_accept_suggestion(self):
        item = BacklogItem.objects.create(
            user=self.user, title="A book", media_type=MediaType.BOOK, estimated_minutes=600
        )
        suggestion = Suggestion.objects.create(
            user=self.user, backlog_item=item, date=timezone.localdate() + timedelta(days=1),
            start_time=time(19), end_time=time(20), minutes=60,
        )
        self.client.force_login(self.user)
        self.client.post(f"/suggestions/{suggestion.pk}/accept/")

        suggestion.refresh_from_db()
        item.refresh_from_db()
        self.assertEqual(suggestion.status, Suggestion.Status.ACCEPTED)
        self.assertEqual(item.status, BacklogItem.Status.IN_PROGRESS)

    def test_mark_done_logs_progress(self):
        item = BacklogItem.objects.create(
            user=self.user, title="A book", media_type=MediaType.BOOK, estimated_minutes=600
        )
        suggestion = Suggestion.objects.create(
            user=self.user, backlog_item=item, date=timezone.localdate(),
            start_time=time(19), end_time=time(20), minutes=60, status=Suggestion.Status.ACCEPTED,
        )
        self.client.force_login(self.user)
        self.client.post(f"/suggestions/{suggestion.pk}/done/")

        item.refresh_from_db()
        self.assertEqual(item.minutes_completed, 60)
        self.assertEqual(item.status, BacklogItem.Status.IN_PROGRESS)

    def test_cannot_touch_another_users_suggestion(self):
        other = make_user("mallory")
        item = BacklogItem.objects.create(
            user=other, title="Theirs", media_type=MediaType.BOOK, estimated_minutes=100
        )
        suggestion = Suggestion.objects.create(
            user=other, backlog_item=item, date=timezone.localdate(),
            start_time=time(19), end_time=time(20), minutes=60,
        )
        self.client.force_login(self.user)
        self.assertEqual(self.client.post(f"/suggestions/{suggestion.pk}/accept/").status_code, 404)


class ScheduleEditTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.client.force_login(self.user)
        self.block = RecurringBlock.objects.create(
            user=self.user, label="CSE 3311", weekday=TEST_DAY.weekday(),
            start_time=time(11), end_time=time(12, 20),
        )
        self.commitment = Commitment.objects.create(
            user=self.user, title="Group project",
            starts_at=timezone.make_aware(datetime(2030, 3, 5, 18, 0)),
            ends_at=timezone.make_aware(datetime(2030, 3, 5, 20, 0)),
        )

    def block_post(self, **overrides):
        data = {"label": "CSE 3311", "kind": "class", "weekday": TEST_DAY.weekday(),
                "start_time": "13:00", "end_time": "14:20"}
        return self.client.post(f"/schedule/block/{self.block.pk}/edit/", {**data, **overrides})

    def test_edit_block_form_is_prefilled(self):
        response = self.client.get(f"/schedule/block/{self.block.pk}/edit/")
        self.assertContains(response, 'value="CSE 3311"')

    def test_edit_block_saves_and_moves_free_time(self):
        self.assertRedirects(self.block_post(), "/schedule/")
        self.block.refresh_from_db()
        self.assertEqual((self.block.start_time, self.block.end_time), (time(13), time(14, 20)))
        gaps = free_intervals(self.user, TEST_DAY)
        self.assertEqual([(g.start, g.end) for g in gaps], [(time(8), time(13)), (time(14, 20), time(23))])

    def test_edit_block_rejects_end_before_start(self):
        response = self.block_post(start_time="15:00", end_time="14:00")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "End time must be after start time.")
        self.block.refresh_from_db()
        self.assertEqual(self.block.start_time, time(11))

    def test_edit_commitment_shows_local_time_and_saves(self):
        response = self.client.get(f"/schedule/commitment/{self.commitment.pk}/edit/")
        self.assertContains(response, 'value="2030-03-05T18:00"')

        response = self.client.post(
            f"/schedule/commitment/{self.commitment.pk}/edit/",
            {"title": "Group project (moved)", "starts_at": "2030-03-06T09:00", "ends_at": "2030-03-06T10:30"},
        )
        self.assertRedirects(response, "/schedule/")
        self.commitment.refresh_from_db()
        self.assertEqual(self.commitment.title, "Group project (moved)")
        self.assertEqual(timezone.localtime(self.commitment.starts_at).hour, 9)

    def test_edit_commitment_rejects_end_before_start(self):
        response = self.client.post(
            f"/schedule/commitment/{self.commitment.pk}/edit/",
            {"title": "Group project", "starts_at": "2030-03-05T20:00", "ends_at": "2030-03-05T18:00"},
        )
        self.assertEqual(response.status_code, 200)
        self.commitment.refresh_from_db()
        self.assertEqual(timezone.localtime(self.commitment.starts_at).hour, 18)

    def test_delete_commitment(self):
        self.assertRedirects(self.client.post(f"/schedule/commitment/{self.commitment.pk}/delete/"), "/schedule/")
        self.assertFalse(Commitment.objects.filter(pk=self.commitment.pk).exists())

    def test_delete_commitment_needs_post(self):
        self.assertEqual(self.client.get(f"/schedule/commitment/{self.commitment.pk}/delete/").status_code, 405)
        self.assertTrue(Commitment.objects.filter(pk=self.commitment.pk).exists())

    def test_schedule_page_links_to_edit_screens(self):
        response = self.client.get("/schedule/")
        self.assertContains(response, f"/schedule/block/{self.block.pk}/edit/")
        self.assertContains(response, f"/schedule/commitment/{self.commitment.pk}/edit/")

    def test_cannot_touch_another_users_schedule(self):
        other = make_user("mallory")
        block = RecurringBlock.objects.create(
            user=other, label="Theirs", weekday=0, start_time=time(9), end_time=time(10)
        )
        commitment = Commitment.objects.create(
            user=other, title="Theirs", starts_at=timezone.now() + timedelta(days=1),
            ends_at=timezone.now() + timedelta(days=1, hours=1),
        )
        for method, url in [
            (self.client.get, f"/schedule/block/{block.pk}/edit/"),
            (self.client.post, f"/schedule/block/{block.pk}/edit/"),
            (self.client.get, f"/schedule/commitment/{commitment.pk}/edit/"),
            (self.client.post, f"/schedule/commitment/{commitment.pk}/edit/"),
            (self.client.post, f"/schedule/commitment/{commitment.pk}/delete/"),
        ]:
            with self.subTest(url=url):
                self.assertEqual(method(url).status_code, 404)
        self.assertTrue(Commitment.objects.filter(pk=commitment.pk).exists())

    def test_edit_screens_require_login(self):
        self.client.logout()
        for url in [f"/schedule/block/{self.block.pk}/edit/", f"/schedule/commitment/{self.commitment.pk}/edit/"]:
            with self.subTest(url=url):
                self.assertRedirects(self.client.get(url), f"/accounts/login/?next={url}")


class ReminderTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.now = timezone.make_aware(datetime(2026, 10, 6, 18, 0))
        self.item = BacklogItem.objects.create(
            user=self.user, title="Dune", media_type=MediaType.BOOK, estimated_minutes=600
        )

    def session(self, start, status=Suggestion.Status.ACCEPTED, day=None):
        return Suggestion.objects.create(
            user=self.user, backlog_item=self.item, date=day or TEST_DAY, start_time=start,
            end_time=time(23, 59), minutes=60, status=status,
        )

    def titles(self, **kwargs):
        return [r["title"] for r in upcoming_reminders(self.user, now=self.now, **kwargs)]

    def test_session_inside_the_window_is_reminded(self):
        self.session(time(18, 10))
        reminders = upcoming_reminders(self.user, now=self.now, lead=15)
        self.assertEqual([r["title"] for r in reminders], ["Dune"])
        self.assertEqual(reminders[0]["minutes_until"], 10)

    def test_window_edges(self):
        self.session(time(18, 15))   # exactly at the horizon
        self.session(time(18, 16))   # one minute too far out
        self.session(time(17, 59))   # already started
        self.assertEqual(len(upcoming_reminders(self.user, now=self.now, lead=15)), 1)

    def test_only_accepted_sessions_count(self):
        for status in [Suggestion.Status.PENDING, Suggestion.Status.REJECTED, Suggestion.Status.DONE]:
            self.session(time(18, 5), status=status)
        self.assertEqual(self.titles(lead=15), [])

    def test_window_can_cross_midnight(self):
        late = timezone.make_aware(datetime(2026, 10, 6, 23, 55))
        self.session(time(0, 5), day=date(2026, 10, 7))
        reminders = upcoming_reminders(self.user, now=late, lead=15)
        self.assertEqual([r["minutes_until"] for r in reminders], [10])

    def test_commitment_is_reminded(self):
        Commitment.objects.create(
            user=self.user, title="Study group",
            starts_at=self.now + timedelta(minutes=10), ends_at=self.now + timedelta(hours=1),
        )
        reminders = upcoming_reminders(self.user, now=self.now, lead=15)
        self.assertEqual([(r["kind"], r["title"]) for r in reminders], [("commitment", "Study group")])

    def test_sorted_soonest_first_and_key_changes_with_start_time(self):
        later = self.session(time(18, 12))
        sooner = Commitment.objects.create(
            user=self.user, title="Quick call",
            starts_at=self.now + timedelta(minutes=3), ends_at=self.now + timedelta(minutes=30),
        )
        reminders = upcoming_reminders(self.user, now=self.now, lead=15)
        self.assertEqual([r["title"] for r in reminders], ["Quick call", "Dune"])
        old_key = reminders[1]["key"]
        later.start_time = time(18, 14)
        later.save()
        self.assertNotEqual(upcoming_reminders(self.user, now=self.now, lead=15)[1]["key"], old_key)

    def test_other_users_events_are_not_included(self):
        other = make_user("mallory")
        Commitment.objects.create(
            user=other, title="Not yours",
            starts_at=self.now + timedelta(minutes=5), ends_at=self.now + timedelta(hours=1),
        )
        self.assertEqual(self.titles(lead=15), [])

    def test_lead_comes_from_the_profile(self):
        self.session(time(18, 40))
        self.assertEqual(self.titles(), [])
        self.user.profile.notify_lead_minutes = 45
        self.user.profile.save()
        self.assertEqual(self.titles(), ["Dune"])


class ReminderViewTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.client.force_login(self.user)
        start = timezone.localtime() + timedelta(minutes=5)
        item = BacklogItem.objects.create(
            user=self.user, title="Dune", media_type=MediaType.BOOK, estimated_minutes=600
        )
        Suggestion.objects.create(
            user=self.user, backlog_item=item, date=start.date(), start_time=start.time(),
            end_time=start.time(), minutes=60, status=Suggestion.Status.ACCEPTED,
        )

    def test_endpoint_requires_login(self):
        self.client.logout()
        self.assertRedirects(
            self.client.get("/notifications/upcoming/"), "/accounts/login/?next=/notifications/upcoming/"
        )

    def test_endpoint_is_empty_until_the_user_opts_in(self):
        data = self.client.get("/notifications/upcoming/").json()
        self.assertEqual(data, {"enabled": False, "reminders": []})

    def test_endpoint_lists_reminders_once_enabled(self):
        self.user.profile.notify_enabled = True
        self.user.profile.save()
        data = self.client.get("/notifications/upcoming/").json()
        self.assertTrue(data["enabled"])
        self.assertEqual([r["title"] for r in data["reminders"]], ["Dune"])
        self.assertIn("starts in", data["reminders"][0]["body"])

    def test_dashboard_banner_shows_upcoming_session(self):
        self.assertContains(self.client.get("/dashboard/"), "Dune")
        self.assertContains(self.client.get("/dashboard/"), "starts in")

    def test_settings_form_saves(self):
        response = self.client.post(
            "/schedule/", {"save_notifications": "1", "notify_enabled": "on", "notify_lead_minutes": "30"}
        )
        self.assertRedirects(response, "/schedule/")
        self.user.profile.refresh_from_db()
        self.assertTrue(self.user.profile.notify_enabled)
        self.assertEqual(self.user.profile.notify_lead_minutes, 30)

    def test_settings_form_rejects_bad_lead_time(self):
        for value in ["0", "121"]:
            with self.subTest(value=value):
                response = self.client.post(
                    "/schedule/", {"save_notifications": "1", "notify_enabled": "on", "notify_lead_minutes": value}
                )
                self.assertEqual(response.status_code, 200)
                self.user.profile.refresh_from_db()
                self.assertEqual(self.user.profile.notify_lead_minutes, 15)

    def test_unchecking_turns_reminders_off(self):
        self.user.profile.notify_enabled = True
        self.user.profile.save()
        self.client.post("/schedule/", {"save_notifications": "1", "notify_lead_minutes": "15"})
        self.user.profile.refresh_from_db()
        self.assertFalse(self.user.profile.notify_enabled)


class WeekPlanTests(TestCase):
    """suggest_for_week on a fixed week, so results don't depend on the clock."""

    def setUp(self):
        self.user = make_user()

    def add(self, title, media_type, minutes, priority=3, **kwargs):
        return BacklogItem.objects.create(
            user=self.user, title=title, media_type=media_type,
            estimated_minutes=minutes, priority=priority, **kwargs
        )

    def week(self, **kwargs):
        return suggest_for_week(self.user, start=TEST_DAY, persist=False, **kwargs)

    def test_covers_seven_consecutive_days(self):
        self.add("Big book", MediaType.BOOK, 5000)
        plan = self.week()
        self.assertEqual(list(plan), [TEST_DAY + timedelta(days=n) for n in range(7)])
        self.assertTrue(all(plan[day] for day in plan))

    def test_a_film_is_planned_once(self):
        self.add("Long film", MediaType.MOVIE, 150)
        planned = [s for day in self.week().values() for s in day]
        self.assertEqual([s.backlog_item.title for s in planned], ["Long film"])

    def test_never_plans_more_than_what_is_left(self):
        self.add("Short book", MediaType.BOOK, 100)
        planned = [s for day in self.week().values() for s in day]
        # 75 minutes, then the last 25 to finish it. Then nothing.
        self.assertEqual([s.minutes for s in planned], [75, 25])

    def test_progress_already_made_is_respected(self):
        self.add("Nearly done", MediaType.BOOK, 100, minutes_completed=80)
        planned = [s for day in self.week().values() for s in day]
        self.assertEqual([s.minutes for s in planned], [20])

    def test_later_days_prefer_something_different(self):
        self.add("Book A", MediaType.BOOK, 2000)
        self.add("Book B", MediaType.BOOK, 2000)
        plan = self.week()
        titles = [plan[TEST_DAY + timedelta(days=n)][0].backlog_item.title for n in range(2)]
        self.assertEqual(titles, ["Book A", "Book B"])

    def test_accepted_sessions_count_as_already_planned(self):
        item = self.add("Short book", MediaType.BOOK, 100)
        Suggestion.objects.create(
            user=self.user, backlog_item=item, date=TEST_DAY + timedelta(days=3), start_time=time(19),
            end_time=time(20), minutes=100, status=Suggestion.Status.ACCEPTED,
        )
        self.assertFalse(any(self.week().values()))

    def test_days_without_free_time_are_left_empty(self):
        self.add("Big book", MediaType.BOOK, 5000)
        wednesday = TEST_DAY + timedelta(days=1)
        RecurringBlock.objects.create(
            user=self.user, label="Away", weekday=wednesday.weekday(), start_time=time(8), end_time=time(23)
        )
        plan = self.week()
        self.assertEqual(plan[wednesday], [])
        self.assertTrue(plan[TEST_DAY])

    def test_persist_false_writes_nothing(self):
        self.add("Big book", MediaType.BOOK, 5000)
        self.week()
        self.assertEqual(Suggestion.objects.count(), 0)

    def test_persist_true_saves_every_suggestion(self):
        self.add("Big book", MediaType.BOOK, 5000)
        plan = suggest_for_week(self.user, start=TEST_DAY)
        self.assertEqual(Suggestion.objects.count(), sum(len(day) for day in plan.values()))

    def test_single_day_behaviour_is_unchanged(self):
        self.add("Big book", MediaType.BOOK, 5000)
        self.assertEqual(len(suggest_for_day(self.user, TEST_DAY, persist=False)), 1)


class WeekViewTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.client.force_login(self.user)
        self.today = timezone.localdate()
        self.book = BacklogItem.objects.create(
            user=self.user, title="Dune", media_type=MediaType.BOOK, estimated_minutes=5000
        )

    def pending(self):
        return Suggestion.objects.filter(user=self.user, status=Suggestion.Status.PENDING)

    def messages_after(self, url):
        response = self.client.post(url, follow=True)
        return [str(m) for m in response.context["messages"]]

    def test_plan_week_creates_pending_suggestions_in_the_window(self):
        self.assertRedirects(self.client.post("/suggestions/plan-week/"), "/schedule/")
        self.assertGreater(self.pending().count(), 0)
        for suggestion in self.pending():
            self.assertGreaterEqual(suggestion.date, self.today)
            self.assertLessEqual(suggestion.date, self.today + timedelta(days=6))

    def test_plan_week_replaces_old_pending_but_keeps_accepted_and_rejected(self):
        def make(day, status):
            return Suggestion.objects.create(
                user=self.user, backlog_item=self.book, date=self.today + timedelta(days=day),
                start_time=time(8), end_time=time(9), minutes=60, status=status,
            )
        stale = make(2, Suggestion.Status.PENDING)
        accepted = make(3, Suggestion.Status.ACCEPTED)
        rejected = make(1, Suggestion.Status.REJECTED)

        self.client.post("/suggestions/plan-week/")
        self.assertFalse(Suggestion.objects.filter(pk=stale.pk).exists())
        self.assertTrue(Suggestion.objects.filter(pk=accepted.pk).exists())
        self.assertTrue(Suggestion.objects.filter(pk=rejected.pk).exists())

    def test_plan_week_leaves_other_users_alone(self):
        other = make_user("mallory")
        item = BacklogItem.objects.create(user=other, title="Theirs", media_type=MediaType.BOOK, estimated_minutes=500)
        theirs = Suggestion.objects.create(
            user=other, backlog_item=item, date=self.today + timedelta(days=1),
            start_time=time(8), end_time=time(9), minutes=60,
        )
        self.client.post("/suggestions/plan-week/")
        self.assertTrue(Suggestion.objects.filter(pk=theirs.pk, status=Suggestion.Status.PENDING).exists())

    def test_plan_week_is_post_only_and_needs_login(self):
        self.assertEqual(self.client.get("/suggestions/plan-week/").status_code, 405)
        self.client.logout()
        self.assertRedirects(
            self.client.post("/suggestions/plan-week/"), "/accounts/login/?next=/suggestions/plan-week/"
        )

    def test_plan_week_says_when_the_backlog_is_empty(self):
        self.book.delete()
        self.assertIn("Add something to your backlog first", " ".join(self.messages_after("/suggestions/plan-week/")))

    def test_plan_week_says_when_there_is_no_free_time(self):
        self.user.profile.day_start, self.user.profile.day_end = time(22, 45), time(23)
        self.user.profile.save()
        self.assertIn("No usable free time", " ".join(self.messages_after("/suggestions/plan-week/")))

    def test_plan_week_says_when_nothing_fits(self):
        self.book.delete()
        BacklogItem.objects.create(user=self.user, title="Epic", media_type=MediaType.MOVIE, estimated_minutes=1200)
        self.assertIn("Nothing in your backlog fits", " ".join(self.messages_after("/suggestions/plan-week/")))

    def test_schedule_page_shows_the_plan_and_the_buttons(self):
        self.client.post("/suggestions/plan-week/")
        response = self.client.get("/schedule/")
        self.assertContains(response, "Plan my week")
        self.assertContains(response, "Dune")
        self.assertContains(response, f"Add all {self.pending().count()} to calendar")

    def test_schedule_page_hides_add_all_when_nothing_is_pending(self):
        self.assertNotContains(self.client.get("/schedule/"), "to calendar")

    def test_accept_week_accepts_everything_pending(self):
        self.client.post("/suggestions/plan-week/")
        waiting = self.pending().count()
        self.assertGreater(waiting, 0)

        self.assertRedirects(self.client.post("/suggestions/accept-week/"), "/schedule/")
        self.assertEqual(self.pending().count(), 0)
        self.assertEqual(Suggestion.objects.filter(status=Suggestion.Status.ACCEPTED).count(), waiting)
        self.book.refresh_from_db()
        self.assertEqual(self.book.status, BacklogItem.Status.IN_PROGRESS)
        self.assertTrue(all(s.responded_at for s in Suggestion.objects.all()))

    def test_accept_week_ignores_other_users_and_later_weeks(self):
        other = make_user("mallory")
        item = BacklogItem.objects.create(user=other, title="Theirs", media_type=MediaType.BOOK, estimated_minutes=500)
        theirs = Suggestion.objects.create(
            user=other, backlog_item=item, date=self.today + timedelta(days=1),
            start_time=time(8), end_time=time(9), minutes=60,
        )
        later = Suggestion.objects.create(
            user=self.user, backlog_item=self.book, date=self.today + timedelta(days=9),
            start_time=time(8), end_time=time(9), minutes=60,
        )
        self.client.post("/suggestions/accept-week/")
        theirs.refresh_from_db()
        later.refresh_from_db()
        self.assertEqual(theirs.status, Suggestion.Status.PENDING)
        self.assertEqual(later.status, Suggestion.Status.PENDING)

    def test_accept_week_with_nothing_pending(self):
        self.assertIn("Nothing waiting", " ".join(self.messages_after("/suggestions/accept-week/")))

    def test_accept_week_is_post_only(self):
        self.assertEqual(self.client.get("/suggestions/accept-week/").status_code, 405)


class VerificationTests(TestCase):
    """Sign-up now ends on a holding page until the emailed link is opened."""

    link_pattern = re.compile(r"http://testserver(/verify/\S+)")

    def register(self, username="student", **overrides):
        data = {
            "username": username, "email": f"{username}@mavs.uta.edu",
            "password1": "sturdy-passphrase-1", "password2": "sturdy-passphrase-1",
        }
        return self.client.post("/register/", {**data, **overrides})

    def link_in(self, message):
        return self.link_pattern.search(message.body).group(1)

    def profile(self, username="student"):
        return Profile.objects.get(user__username=username)

    def test_registering_sends_one_email_with_a_link(self):
        self.assertRedirects(self.register(), "/verify/")
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["student@mavs.uta.edu"])
        self.assertRegex(mail.outbox[0].body, self.link_pattern)
        self.assertFalse(self.profile().email_verified)
        self.assertIsNotNone(self.profile().verification_sent_at)

    def test_holding_page_names_the_address(self):
        response = self.client.get("/verify/", follow=False)
        self.assertEqual(response.status_code, 302)  # not signed in yet
        self.register()
        self.assertContains(self.client.get("/verify/"), "student@mavs.uta.edu")

    def test_unverified_users_cannot_reach_the_app(self):
        self.register()
        for url in ["/dashboard/", "/calendar/", "/backlog/", "/discover/", "/schedule/", "/reviews/", "/"]:
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertRedirects(response, "/verify/", fetch_redirect_response=False)

    def test_unverified_users_can_still_sign_out(self):
        self.register()
        self.assertEqual(self.client.post("/accounts/logout/").status_code, 302)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_unverified_user_signing_in_again_lands_on_the_holding_page(self):
        self.register()
        self.client.post("/accounts/logout/")
        self.client.post("/accounts/login/", {"username": "student", "password": "sturdy-passphrase-1"})
        self.assertRedirects(self.client.get("/dashboard/"), "/verify/")

    def test_the_link_unlocks_the_account(self):
        self.register()
        response = self.client.get(self.link_in(mail.outbox[0]))
        self.assertRedirects(response, "/schedule/")
        self.assertTrue(self.profile().email_verified)
        self.assertEqual(self.client.get("/dashboard/").status_code, 200)

    def test_the_link_works_in_another_browser(self):
        self.register()
        link = self.link_in(mail.outbox[0])
        self.client.logout()
        response = self.client.get(link)
        self.assertRedirects(response, "/accounts/login/")
        self.assertTrue(self.profile().email_verified)

    def test_opening_the_link_twice_is_harmless(self):
        self.register()
        link = self.link_in(mail.outbox[0])
        self.client.get(link)
        response = self.client.get(link, follow=True)
        self.assertTrue(self.profile().email_verified)
        self.assertIn("already confirmed", " ".join(str(m) for m in response.context["messages"]))

    def test_a_forged_link_is_rejected(self):
        self.register()
        token = self.link_in(mail.outbox[0]).strip("/").split("/")[-1]
        for bad in [token + "x", "not-a-token", token[:-6], token.replace(":", "", 1)]:
            with self.subTest(token=bad):
                self.assertEqual(self.client.get(f"/verify/{bad}/").status_code, 400)
        self.assertFalse(self.profile().email_verified)

    def test_a_token_signed_with_a_different_salt_is_rejected(self):
        self.register()
        user = get_user_model().objects.get(username="student")
        forged = signing.dumps({"uid": user.pk, "email": user.email}, salt="something-else")
        self.assertEqual(self.client.get(f"/verify/{forged}/").status_code, 400)
        self.assertFalse(self.profile().email_verified)

    def test_an_old_link_expires(self):
        self.register()
        link = self.link_in(mail.outbox[0])
        later = timezone.now().timestamp() + (TOKEN_MAX_AGE_DAYS * 24 * 60 * 60) + 60
        with mock.patch("django.core.signing.time.time", return_value=later):
            self.assertEqual(self.client.get(link).status_code, 400)
        self.assertFalse(self.profile().email_verified)

    def test_a_link_still_works_just_inside_the_limit(self):
        self.register()
        link = self.link_in(mail.outbox[0])
        almost = timezone.now().timestamp() + (TOKEN_MAX_AGE_DAYS * 24 * 60 * 60) - 60
        with mock.patch("django.core.signing.time.time", return_value=almost):
            self.assertEqual(self.client.get(link).status_code, 302)
        self.assertTrue(self.profile().email_verified)

    def test_a_link_stops_working_if_the_email_changes(self):
        self.register()
        link = self.link_in(mail.outbox[0])
        user = get_user_model().objects.get(username="student")
        user.email = "other@mavs.uta.edu"
        user.save()
        self.assertEqual(self.client.get(link).status_code, 400)
        self.assertFalse(self.profile().email_verified)

    def test_a_link_for_a_deleted_account_is_rejected(self):
        self.register()
        link = self.link_in(mail.outbox[0])
        get_user_model().objects.get(username="student").delete()
        self.assertEqual(self.client.get(link).status_code, 400)

    def test_a_link_only_verifies_the_account_it_was_issued_for(self):
        self.register("alice")
        self.client.post("/accounts/logout/")
        self.register("bob")
        alice_link = self.link_in(mail.outbox[0])
        self.client.get(alice_link)
        self.assertTrue(self.profile("alice").email_verified)
        self.assertFalse(self.profile("bob").email_verified)

    def test_resend_sends_another_email_after_the_cooldown(self):
        self.register()
        Profile.objects.filter(user__username="student").update(
            verification_sent_at=timezone.now() - timedelta(seconds=90)
        )
        self.assertRedirects(self.client.post("/verify/resend/"), "/verify/")
        self.assertEqual(len(mail.outbox), 2)
        # Both links are good. The old one isn't revoked by asking for a new one.
        self.client.get(self.link_in(mail.outbox[0]))
        self.assertTrue(self.profile().email_verified)

    def test_resend_is_rate_limited(self):
        self.register()
        response = self.client.post("/verify/resend/", follow=True)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("Please wait", " ".join(str(m) for m in response.context["messages"]))

    def test_holding_page_shows_the_wait(self):
        self.register()
        self.assertContains(self.client.get("/verify/"), "You can ask again in")

    def test_resend_needs_login_and_post(self):
        self.assertRedirects(self.client.post("/verify/resend/"), "/accounts/login/?next=/verify/resend/")
        self.register()
        self.assertEqual(self.client.get("/verify/resend/").status_code, 405)

    def test_a_mail_failure_does_not_lose_the_account(self):
        with mock.patch("planner.verification.send_mail", side_effect=smtplib.SMTPException("down")):
            response = self.register()
        self.assertRedirects(response, "/verify/")
        self.assertTrue(get_user_model().objects.filter(username="student").exists())
        # No email went out, so there's no cooldown standing in the way of a retry.
        self.assertIsNone(self.profile().verification_sent_at)
        self.client.post("/verify/resend/")
        self.assertEqual(len(mail.outbox), 1)

    def test_a_mail_failure_on_resend_is_reported(self):
        self.register()
        Profile.objects.filter(user__username="student").update(verification_sent_at=None)
        with mock.patch("planner.verification.send_mail", side_effect=OSError("no route")):
            response = self.client.post("/verify/resend/", follow=True)
        self.assertIn("couldn't send", " ".join(str(m) for m in response.context["messages"]))

    def test_verified_users_are_sent_away_from_the_holding_page(self):
        self.client.force_login(make_user())
        self.assertRedirects(self.client.get("/verify/"), "/dashboard/")
        self.assertRedirects(self.client.post("/verify/resend/"), "/dashboard/")

    def test_staff_are_not_held_back(self):
        admin = get_user_model().objects.create_superuser("boss", "boss@example.com", "pw")
        self.client.force_login(admin)
        self.assertEqual(self.client.get("/dashboard/").status_code, 200)

    def test_the_reminder_poll_is_not_redirected_for_unverified_users(self):
        self.register()
        response = self.client.get("/notifications/upcoming/")
        self.assertEqual(response.json(), {"enabled": False, "reminders": []})

    def test_the_uta_email_rule_still_applies(self):
        response = self.register(email="me@gmail.com")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(mail.outbox), 0)
        self.assertFalse(get_user_model().objects.filter(username="student").exists())

    def test_the_demo_account_is_verified(self):
        call_command("seed_catalog", demo=True, stdout=io.StringIO())
        self.assertTrue(Profile.objects.get(user__username="demo").email_verified)


class NavVisibilityTests(TestCase):
    def test_unverified_users_do_not_see_app_links(self):
        user = make_user(email_verified=False)
        self.client.force_login(user)
        response = self.client.get("/verify/")
        self.assertNotContains(response, 'href="/calendar/"')
        self.assertContains(response, "Sign out")

    def test_verified_users_see_app_links(self):
        self.client.force_login(make_user())
        self.assertContains(self.client.get("/dashboard/"), 'href="/calendar/"')

    def test_staff_without_a_profile_see_app_links(self):
        admin = get_user_model().objects.create_superuser("boss", "boss@example.com", "pw")
        self.client.force_login(admin)
        self.assertContains(self.client.get("/dashboard/"), 'href="/calendar/"')


@override_settings(DEBUG=True)
class ResetAccountsTests(TestCase):
    def setUp(self):
        self.student = make_user("student")
        self.demo = make_user("demo")
        self.admin = get_user_model().objects.create_superuser("boss", "boss@example.com", "pw")
        BacklogItem.objects.create(user=self.student, title="Dune", media_type=MediaType.BOOK, estimated_minutes=600)
        CatalogItem.objects.create(title="Shared film", media_type=MediaType.MOVIE, typical_minutes=100)

    def run_command(self, *args):
        out = io.StringIO()
        call_command("reset_accounts", *args, stdout=out)
        return out.getvalue()

    def usernames(self):
        return set(get_user_model().objects.values_list("username", flat=True))

    def test_deletes_students_and_their_data_but_keeps_staff_and_catalog(self):
        self.run_command("--yes")
        self.assertEqual(self.usernames(), {"boss"})
        self.assertEqual(BacklogItem.objects.count(), 0)
        self.assertEqual(Profile.objects.count(), 0)
        self.assertEqual(CatalogItem.objects.count(), 1)

    def test_keep_demo(self):
        self.run_command("--yes", "--keep-demo")
        self.assertEqual(self.usernames(), {"demo", "boss"})

    def test_include_staff(self):
        self.run_command("--yes", "--include-staff")
        self.assertEqual(self.usernames(), set())

    def test_the_same_address_can_register_again_afterwards(self):
        self.run_command("--yes")
        response = self.client.post(
            "/register/",
            {"username": "student", "email": "student@mavs.uta.edu",
             "password1": "sturdy-passphrase-1", "password2": "sturdy-passphrase-1"},
        )
        self.assertRedirects(response, "/verify/")
        self.assertEqual(len(mail.outbox), 1)

    def test_asks_first_and_stops_unless_told_yes(self):
        with mock.patch("builtins.input", return_value="no"):
            output = self.run_command()
        self.assertIn("Nothing deleted", output)
        self.assertEqual(self.usernames(), {"student", "demo", "boss"})

    def test_continues_when_told_yes_at_the_prompt(self):
        with mock.patch("builtins.input", return_value="yes"):
            self.run_command()
        self.assertEqual(self.usernames(), {"boss"})

    def test_lists_what_it_will_delete(self):
        output = self.run_command("--yes")
        self.assertIn("student  student@mavs.uta.edu", output)
        self.assertNotIn("boss", output)

    def test_nothing_to_do(self):
        self.run_command("--yes", "--include-staff")
        self.assertIn("No accounts to delete", self.run_command("--yes"))

    @override_settings(DEBUG=False)
    def test_refuses_when_debug_is_off(self):
        with self.assertRaises(CommandError):
            self.run_command("--yes")
        self.assertEqual(self.usernames(), {"student", "demo", "boss"})


class EnvFileTests(TestCase):
    def write(self, text, encoding="utf-8"):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / ".env"
        path.write_bytes(text.encode(encoding))
        return path

    def test_reads_names_and_values(self):
        path = self.write('A=1\nB = "two words"\nC=\'single\'\nD=plain\n')
        self.assertEqual(parse_env_file(path), {"A": "1", "B": "two words", "C": "single", "D": "plain"})

    def test_skips_blank_lines_comments_and_junk(self):
        path = self.write("# a comment\n\n   \nnot a setting\n=orphan\nA=1\n")
        self.assertEqual(parse_env_file(path), {"A": "1"})

    def test_accepts_export_prefix(self):
        self.assertEqual(parse_env_file(self.write("export A=1\n")), {"A": "1"})

    def test_quoted_values_keep_hashes_and_equals_signs(self):
        path = self.write('A="pass#word=1"\nB="two  spaces"\n')
        self.assertEqual(parse_env_file(path), {"A": "pass#word=1", "B": "two  spaces"})

    def test_unquoted_values_can_have_trailing_comments(self):
        path = self.write("A=1 # one\nB=x#y\n")
        self.assertEqual(parse_env_file(path), {"A": "1", "B": "x#y"})

    def test_values_may_contain_angle_brackets_and_equals_signs(self):
        path = self.write('FROM="MediaFlow <team@example.com>"\nKEY=abc==\n')
        self.assertEqual(parse_env_file(path), {"FROM": "MediaFlow <team@example.com>", "KEY": "abc=="})

    def test_handles_a_byte_order_mark_and_windows_line_endings(self):
        path = self.write("﻿A=1\r\nB=2\r\n")
        self.assertEqual(parse_env_file(path), {"A": "1", "B": "2"})

    def test_a_missing_file_is_fine(self):
        self.assertEqual(parse_env_file(Path(tempfile.gettempdir()) / "no-such-dir" / ".env"), {})

    def test_load_sets_variables(self):
        path = self.write("MEDIAFLOW_TEST_A=hello\n")
        with mock.patch.dict(os.environ, clear=False):
            os.environ.pop("MEDIAFLOW_TEST_A", None)
            load_env_file(path)
            self.assertEqual(os.environ["MEDIAFLOW_TEST_A"], "hello")

    def test_real_environment_variables_win_over_the_file(self):
        path = self.write("MEDIAFLOW_TEST_A=from-file\n")
        with mock.patch.dict(os.environ, {"MEDIAFLOW_TEST_A": "from-terminal"}):
            load_env_file(path)
            self.assertEqual(os.environ["MEDIAFLOW_TEST_A"], "from-terminal")

    def test_the_example_file_parses_and_holds_only_placeholders(self):
        values = parse_env_file(Path(__file__).resolve().parent.parent / ".env.example")
        self.assertEqual(values["EMAIL_HOST"], "smtp.gmail.com")
        self.assertIn("@gmail.com", values["EMAIL_HOST_USER"])
        self.assertTrue(values["EMAIL_HOST_USER"].startswith("your."))
        self.assertEqual(values["EMAIL_HOST_PASSWORD"], "abcd efgh ijkl mnop")

    def test_gmail_app_passwords_are_used_without_the_spaces_google_shows(self):
        # Settings strips spaces for Gmail, since Google displays the code in groups.
        self.assertEqual(clean_smtp_password("smtp.gmail.com", "abcd efgh ijkl mnop"), "abcdefghijklmnop")
        self.assertEqual(clean_smtp_password("smtp.example.com", "has a space"), "has a space")


# ---------------------------------------------------------------- catalog import


class FakeFetcher:
    """Stands in for HttpFetcher. `routes` maps a URL path to a response, or to a
    function of the query parameters. An unexpected URL fails the test."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def get_json(self, url, params=None, headers=None):
        self.calls.append((url, dict(params or {}), dict(headers or {})))
        path = urllib.parse.urlparse(url).path
        if path not in self.routes:
            raise AssertionError(f"unexpected request to {url}")
        response = self.routes[path]
        return response(dict(params or {})) if callable(response) else response

    def paths(self):
        return [urllib.parse.urlparse(url).path for url, _, _ in self.calls]


def first_page_only(rows, key="results"):
    """A list endpoint with one page of rows, then nothing."""
    return lambda params: {key: rows if params.get("page") == 1 else []}


class ImportHelperTests(TestCase):
    def test_genre_names_are_made_to_match_the_seed_data(self):
        self.assertEqual(normalize_genre("Science Fiction"), "Science fiction")
        self.assertEqual(normalize_genre("Sci-Fi & Fantasy"), "Science fiction")
        self.assertEqual(normalize_genre("Action & Adventure"), "Action")
        self.assertEqual(normalize_genre("  Drama "), "Drama")
        self.assertEqual(normalize_genre(None), "")

    def test_description_stops_at_the_rule_and_the_first_paragraph(self):
        text = "A desert planet.\n\n----------\nAlso by this author"
        self.assertEqual(tidy_description(text), "A desert planet.")
        self.assertEqual(tidy_description("One.\n\nTwo."), "One.")
        self.assertEqual(tidy_description(None), "")

    def test_long_description_is_cut_at_a_sentence(self):
        text = "A sentence that goes on. " * 60
        cut = tidy_description(text, limit=100)
        self.assertLessEqual(len(cut), 100)
        self.assertTrue(cut.endswith("."))

    def test_long_description_without_a_stop_gets_an_ellipsis(self):
        self.assertTrue(tidy_description("word " * 400, limit=100).endswith("…"))

    def test_ratings_are_scaled_and_zero_means_none(self):
        self.assertEqual(one_decimal(4.31, scale=2), 8.6)
        self.assertEqual(one_decimal(78, scale=0.1), 7.8)
        self.assertEqual(one_decimal(7.64), 7.6)
        self.assertIsNone(one_decimal(0))
        self.assertIsNone(one_decimal(None))
        self.assertIsNone(one_decimal("n/a"))

    def test_dates_and_clipping(self):
        self.assertEqual(parse_date("2016-11-11"), date(2016, 11, 11))
        self.assertEqual(parse_date("2016-11-11T00:00:00"), date(2016, 11, 11))
        self.assertIsNone(parse_date(""))
        self.assertIsNone(parse_date("soon"))
        self.assertEqual(clip("abcdef", 4), "abc…")
        self.assertEqual(clip("  abc  ", 10), "abc")

    def test_a_book_is_filed_under_the_genre_its_tags_point_to_most(self):
        tags = ["Fantasy fiction", "Fantasy", "Humorous stories", "Juvenile fiction, fantasy"]
        self.assertEqual(best_book_label(tags), "Fantasy")
        self.assertEqual(best_book_label(["SCIENCE FICTION"]), "Science fiction")

    def test_a_tie_goes_to_the_earlier_genre(self):
        self.assertEqual(best_book_label(["Fantasy", "Horror"]), "Horror")

    def test_no_matching_tags_means_no_genre(self):
        self.assertIsNone(best_book_label(["Cooking", "Gardening"]))
        self.assertIsNone(best_book_label([]))
        self.assertIsNone(best_book_label(None))


def fake_response(payload):
    class Response:
        def read(self):
            return json.dumps(payload).encode()

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    return Response()


def http_error(code, retry_after=None):
    headers = Message()
    if retry_after is not None:
        headers["Retry-After"] = str(retry_after)
    return urllib.error.HTTPError("https://api.example.com/x", code, "error", headers, None)


class HttpFetcherTests(TestCase):
    def setUp(self):
        self.sleeps = []
        self.fetcher = HttpFetcher("MediaFlow-Test (me@example.com)", pause=0.5, sleep=self.sleeps.append)

    def get(self, *responses, **kwargs):
        with mock.patch("planner.catalog_import.urllib.request.urlopen", side_effect=list(responses)) as opened:
            try:
                return self.fetcher.get_json("https://api.example.com/x", **kwargs), opened
            except ImportFailure as error:
                return error, opened

    def test_returns_parsed_json_and_identifies_itself(self):
        result, opened = self.get(fake_response({"ok": True}), params={"page": 2, "skip": None})
        self.assertEqual(result, {"ok": True})
        request = opened.call_args[0][0]
        self.assertEqual(request.full_url, "https://api.example.com/x?page=2")
        self.assertEqual(request.get_header("User-agent"), "MediaFlow-Test (me@example.com)")

    def test_every_call_is_paced(self):
        self.get(fake_response({}))
        self.assertEqual(self.sleeps, [0.5])

    def test_extra_headers_are_sent(self):
        _, opened = self.get(fake_response({}), headers={"Authorization": "Bearer abc"})
        self.assertEqual(opened.call_args[0][0].get_header("Authorization"), "Bearer abc")

    def test_not_found_is_none(self):
        result, _ = self.get(http_error(404))
        self.assertIsNone(result)

    def test_a_refused_key_stops_the_import_and_does_not_leak_the_key(self):
        result, opened = self.get(http_error(401), params={"api_key": "SECRET-KEY-123"})
        self.assertIsInstance(result, ImportFailure)
        self.assertIn("API key", str(result))
        self.assertNotIn("SECRET-KEY-123", str(result))
        self.assertEqual(opened.call_count, 1)

    def test_rate_limiting_waits_as_told_and_retries(self):
        result, opened = self.get(http_error(429, retry_after=7), fake_response({"ok": 1}))
        self.assertEqual(result, {"ok": 1})
        self.assertEqual(opened.call_count, 2)
        self.assertIn(7, self.sleeps)

    def test_server_errors_back_off_then_give_up(self):
        result, opened = self.get(http_error(503), http_error(503), http_error(503))
        self.assertIsInstance(result, ImportFailure)
        self.assertIn("Gave up", str(result))
        self.assertEqual(opened.call_count, 3)
        self.assertEqual([s for s in self.sleeps if s >= 2], [2, 4, 8])

    def test_a_dropped_connection_is_retried(self):
        result, _ = self.get(urllib.error.URLError("reset"), fake_response({"ok": 1}))
        self.assertEqual(result, {"ok": 1})

    def test_other_client_errors_fail_at_once(self):
        result, opened = self.get(http_error(400))
        self.assertIsInstance(result, ImportFailure)
        self.assertEqual(opened.call_count, 1)


BOOK_DOCS = [
    {"key": "/works/OL1W", "title": "Dune", "author_name": ["Frank Herbert"], "first_publish_year": 1965,
     "number_of_pages_median": 500, "ratings_average": 4.3, "ratings_count": 900,
     "subject": ["Science fiction", "Science fiction, fiction", "Ecology"]},
    {"key": "/works/OL2W", "title": "The Magic Door", "author_name": ["A. Writer"], "first_publish_year": 1999,
     "number_of_pages_median": 300, "subject": ["Fantasy fiction", "Fantasy", "Humorous stories"]},
    {"key": "/works/OL3W", "title": "Tiny Pamphlet", "author_name": ["B. Writer"], "number_of_pages_median": 30,
     "subject": ["Fantasy"]},
    {"key": "/works/OL4W", "title": "Anonymous", "number_of_pages_median": 300, "subject": ["Fantasy"]},
    {"key": "/works/OL1W", "title": "Dune", "author_name": ["Frank Herbert"], "number_of_pages_median": 500,
     "subject": ["Science fiction"]},
    {"key": "/works/OL5W", "title": "dune", "author_name": ["frank herbert"], "number_of_pages_median": 400,
     "subject": ["Science fiction"]},
    {"key": "/works/OL6W", "title": "Cookbook", "author_name": ["C. Cook"], "number_of_pages_median": 200,
     "subject": ["Cooking"]},
]


def book_routes():
    return {
        "/search.json": first_page_only(BOOK_DOCS, key="docs"),
        "/works/OL1W.json": {"description": {"type": "/type/text", "value": "A desert planet.\n\n----------\nExtra"}},
        "/works/OL2W.json": {"description": "A door."},
    }


class BookSourceTests(TestCase):
    def run_source(self, limit):
        fetcher = FakeFetcher(book_routes())
        source = BookSource(fetcher)
        return source, fetcher, list(source.records(limit))

    def test_builds_records_from_search_results(self):
        _, _, records = self.run_source(28)
        by_title = {r.title: r for r in records}
        self.assertEqual(set(by_title), {"Dune", "The Magic Door"})
        self.assertEqual(
            by_title["Dune"],
            Record(
                source="openlibrary", source_id="OL1W", title="Dune", media_type="book", typical_minutes=650,
                genre="Science fiction", creator="Frank Herbert", description="A desert planet.",
                release_date=date(1965, 1, 1), rating=8.6,
            ),
        )

    def test_reading_time_is_pages_at_a_steady_pace(self):
        _, _, records = self.run_source(28)
        self.assertEqual({r.title: r.typical_minutes for r in records}, {"Dune": 650, "The Magic Door": 390})

    def test_missing_fields_are_left_empty_rather_than_invented(self):
        _, _, records = self.run_source(28)
        door = next(r for r in records if r.title == "The Magic Door")
        self.assertIsNone(door.rating)
        self.assertEqual(door.description, "A door.")

    def test_unusable_and_duplicate_books_are_counted_not_imported(self):
        source, _, _ = self.run_source(28)
        self.assertEqual(
            dict(source.skipped), {"no usable page count": 1, "no title or author": 1, "duplicate work": 1}
        )

    def test_books_with_no_matching_genre_are_ignored(self):
        _, fetcher, records = self.run_source(28)
        self.assertNotIn("Cookbook", [r.title for r in records])
        self.assertNotIn("/works/OL6W.json", fetcher.paths())

    def test_stops_at_the_limit(self):
        _, _, records = self.run_source(1)
        self.assertEqual(len(records), 1)

    def test_asks_for_popular_english_books_in_each_subject(self):
        _, fetcher, _ = self.run_source(28)
        searches = [params for url, params, _ in fetcher.calls if url.endswith("/search.json")]
        self.assertTrue(all("language:eng" in p["q"] and p["sort"] == "readinglog" for p in searches))
        self.assertIn("subject:science_fiction language:eng", [p["q"] for p in searches])

    def test_needs_no_key(self):
        self.assertEqual(BookSource.key_env, "")


def tmdb_lists(kind, top, popular):
    return {
        f"/3/{kind}/top_rated": first_page_only([{"id": i} for i in top]),
        f"/3/{kind}/popular": first_page_only([{"id": i} for i in popular]),
    }


MOVIE_DETAILS = {
    1: {"title": "Arrival", "runtime": 116, "overview": "A linguist is recruited.", "release_date": "2016-11-11",
        "vote_average": 7.64, "genres": [{"name": "Science Fiction"}, {"name": "Drama"}],
        "credits": {"crew": [{"job": "Producer", "name": "Someone"}, {"job": "Director", "name": "Denis Villeneuve"}]}},
    2: {"title": "No Runtime", "runtime": 0, "genres": []},
    3: {"title": "Parasite", "runtime": 132, "overview": "", "release_date": "", "vote_average": 0,
        "genres": [], "credits": {"crew": []}},
    4: {"title": "A Short", "runtime": 15, "genres": []},
}


class MovieSourceTests(TestCase):
    def source(self, key="abc123"):
        routes = {
            **tmdb_lists("movie", top=[1, 2, 3], popular=[3, 4, 5]),
            **{f"/3/movie/{i}": d for i, d in MOVIE_DETAILS.items()},
            "/3/movie/5": None,
        }
        fetcher = FakeFetcher(routes)
        return MovieSource(fetcher, key), fetcher

    def test_builds_a_record_from_details_and_credits(self):
        source, _ = self.source()
        arrival = next(r for r in source.records(10) if r.title == "Arrival")
        self.assertEqual(
            arrival,
            Record(
                source="tmdb", source_id="movie-1", title="Arrival", media_type="movie", typical_minutes=116,
                genre="Science fiction", creator="Denis Villeneuve", description="A linguist is recruited.",
                release_date=date(2016, 11, 11), rating=7.6,
            ),
        )

    def test_empty_values_stay_empty(self):
        source, _ = self.source()
        parasite = next(r for r in source.records(10) if r.title == "Parasite")
        self.assertEqual((parasite.genre, parasite.creator, parasite.description), ("", "", ""))
        self.assertIsNone(parasite.release_date)
        self.assertIsNone(parasite.rating)

    def test_drops_missing_runtimes_shorts_and_missing_titles(self):
        source, _ = self.source()
        self.assertEqual([r.title for r in source.records(10)], ["Arrival", "Parasite"])
        self.assertEqual(dict(source.skipped), {"no usable runtime": 2, "details not found": 1})

    def test_a_title_in_both_lists_is_fetched_once(self):
        source, fetcher = self.source()
        list(source.records(10))
        self.assertEqual(fetcher.paths().count("/3/movie/3"), 1)

    def test_stops_at_the_limit(self):
        source, _ = self.source()
        self.assertEqual(len(list(source.records(1))), 1)

    def test_a_short_key_goes_in_the_query(self):
        source, fetcher = self.source("abc123")
        list(source.records(1))
        _, params, headers = fetcher.calls[0]
        self.assertEqual(params["api_key"], "abc123")
        self.assertNotIn("Authorization", headers)

    def test_a_long_access_token_goes_in_a_header(self):
        token = "eyJhbGciOiJIUzI1NiJ9." + "x" * 60
        source, fetcher = self.source(token)
        list(source.records(1))
        _, params, headers = fetcher.calls[0]
        self.assertEqual(headers["Authorization"], f"Bearer {token}")
        self.assertNotIn("api_key", params)

    def test_stops_when_the_lists_run_out(self):
        source, _ = self.source()
        self.assertEqual(len(list(source.records(500))), 2)


TV_DETAILS = {
    10: {"name": "Breaking Bad", "number_of_episodes": 62, "episode_run_time": [47], "overview": "A teacher.",
         "first_air_date": "2008-01-20", "vote_average": 8.9, "genres": [{"name": "Drama"}, {"name": "Crime"}],
         "created_by": [{"name": "Vince Gilligan"}], "networks": [{"name": "AMC"}]},
    11: {"name": "Fallback Runtime", "number_of_episodes": 10, "episode_run_time": [],
         "last_episode_to_air": {"runtime": 50}, "genres": [{"name": "Sci-Fi & Fantasy"}],
         "created_by": [], "networks": [{"name": "HBO"}]},
    12: {"name": "Late Night", "number_of_episodes": 200, "episode_run_time": [60], "genres": [{"name": "Talk"}]},
    13: {"name": "Forever Soap", "number_of_episodes": 5000, "episode_run_time": [30], "genres": [{"name": "Drama"}]},
    14: {"name": "No Episodes", "number_of_episodes": 0, "episode_run_time": [30], "genres": []},
    15: {"name": "No Runtime Anywhere", "number_of_episodes": 8, "episode_run_time": [], "genres": []},
}


class TvSourceTests(TestCase):
    def records(self):
        routes = {**tmdb_lists("tv", top=[10, 11, 12], popular=[13, 14, 15]),
                  **{f"/3/tv/{i}": d for i, d in TV_DETAILS.items()}}
        source = TvSource(FakeFetcher(routes), "abc123")
        return source, list(source.records(10))

    def test_total_time_is_episodes_times_episode_length(self):
        _, records = self.records()
        bad = next(r for r in records if r.title == "Breaking Bad")
        self.assertEqual(bad.typical_minutes, 62 * 47)
        self.assertEqual((bad.creator, bad.genre, bad.source_id), ("Vince Gilligan", "Drama", "tv-10"))
        self.assertEqual(bad.release_date, date(2008, 1, 20))

    def test_falls_back_to_an_aired_episode_length_and_the_network(self):
        _, records = self.records()
        other = next(r for r in records if r.title == "Fallback Runtime")
        self.assertEqual(other.typical_minutes, 10 * 50)
        self.assertEqual((other.creator, other.genre), ("HBO", "Science fiction"))

    def test_leaves_out_what_nobody_would_plan(self):
        source, records = self.records()
        self.assertEqual([r.title for r in records], ["Breaking Bad", "Fallback Runtime"])
        self.assertEqual(
            dict(source.skipped), {"talk, news or soap": 1, "too long to plan": 1, "no usable runtime": 2}
        )

    def test_shares_the_tmdb_key_with_movies(self):
        self.assertEqual(TvSource.key_env, MovieSource.key_env)


GAME_ROWS = [
    {"id": 100, "name": "Hades", "released": "2020-09-17", "rating": 4.4, "metacritic": 93, "playtime": 22,
     "genres": [{"name": "Action"}, {"name": "Indie"}]},
    {"id": 101, "name": "No Playtime Yet", "playtime": 0, "genres": []},
    {"id": 102, "name": "Marathon", "playtime": 900, "genres": []},
    {"id": 103, "name": "Rated Only", "released": "2018-01-01", "rating": 4.0, "metacritic": None, "playtime": 10,
     "genres": [{"name": "Puzzle"}]},
    {"id": 104, "name": "Detail Playtime", "playtime": 0, "genres": []},
    {"id": 105, "name": ""},
]
GAME_DETAILS = {
    "/api/games/100": {"description_raw": "Escape the underworld.", "developers": [{"name": "Supergiant Games"}],
                       "playtime": 22},
    "/api/games/101": {"playtime": 0},
    "/api/games/102": {"playtime": 900},
    "/api/games/103": {"description_raw": "", "developers": [], "playtime": 10},
    "/api/games/104": {"playtime": 6, "description_raw": "Only the detail knows."},
}


class GameSourceTests(TestCase):
    def source(self):
        routes = {"/api/games": first_page_only(GAME_ROWS), **GAME_DETAILS}
        fetcher = FakeFetcher(routes)
        return GameSource(fetcher, "rawgkey"), fetcher

    def test_playtime_hours_become_minutes(self):
        source, _ = self.source()
        hades = next(r for r in source.records(10) if r.title == "Hades")
        self.assertEqual(
            hades,
            Record(
                source="rawg", source_id="100", title="Hades", media_type="game", typical_minutes=22 * 60,
                genre="Action", creator="Supergiant Games", description="Escape the underworld.",
                release_date=date(2020, 9, 17), rating=9.3,
            ),
        )

    def test_rating_falls_back_to_user_score_doubled(self):
        source, _ = self.source()
        rated = next(r for r in source.records(10) if r.title == "Rated Only")
        self.assertEqual(rated.rating, 8.0)

    def test_playtime_can_come_from_the_detail_call(self):
        source, _ = self.source()
        detail = next(r for r in source.records(10) if r.title == "Detail Playtime")
        self.assertEqual(detail.typical_minutes, 360)

    def test_leaves_out_games_with_no_believable_playtime(self):
        source, _ = self.source()
        self.assertEqual(
            [r.title for r in source.records(10)], ["Hades", "Rated Only", "Detail Playtime"]
        )
        self.assertEqual(dict(source.skipped), {"no usable playtime": 2, "no title": 1})

    def test_sends_the_key_and_asks_for_popular_games(self):
        source, fetcher = self.source()
        list(source.records(1))
        _, params, _ = fetcher.calls[0]
        self.assertEqual((params["key"], params["ordering"], params["page_size"]), ("rawgkey", "-added", 40))


class SaveRecordTests(TestCase):
    def record(self, **overrides):
        values = dict(
            source="tmdb", source_id="movie-1", title="Arrival", media_type="movie", typical_minutes=116,
            genre="Science fiction", creator="Denis Villeneuve", description="A linguist.",
            release_date=date(2016, 11, 11), rating=7.6,
        )
        return Record(**{**values, **overrides})

    def test_creates_a_row_with_every_field(self):
        self.assertEqual(save_record(self.record()), "created")
        item = CatalogItem.objects.get()
        self.assertEqual((item.source, item.source_id, item.title), ("tmdb", "movie-1", "Arrival"))
        self.assertEqual((item.typical_minutes, item.genre, item.creator), (116, "Science fiction", "Denis Villeneuve"))
        self.assertEqual(float(item.rating), 7.6)
        self.assertEqual(item.slug, "arrival-movie")

    def test_running_again_refreshes_the_same_row(self):
        save_record(self.record())
        outcome = save_record(self.record(typical_minutes=120, rating=8.1, title="Arrival (2016)"))
        self.assertEqual(outcome, "updated")
        item = CatalogItem.objects.get()
        self.assertEqual((item.typical_minutes, float(item.rating), item.title), (120, 8.1, "Arrival (2016)"))

    def test_the_same_title_from_another_source_id_is_a_new_row(self):
        save_record(self.record())
        save_record(self.record(source_id="movie-2", release_date=date(1984, 1, 1)))
        self.assertEqual(CatalogItem.objects.count(), 2)
        self.assertEqual(len(set(CatalogItem.objects.values_list("slug", flat=True))), 2)

    def test_a_hand_entered_title_with_the_same_year_wins(self):
        CatalogItem.objects.create(title="arrival", media_type="movie", typical_minutes=116, release_date=date(2016, 1, 1))
        self.assertEqual(save_record(self.record()), "duplicate")
        self.assertEqual(CatalogItem.objects.count(), 1)

    def test_a_hand_entered_title_from_another_year_does_not(self):
        CatalogItem.objects.create(title="Arrival", media_type="movie", typical_minutes=90, release_date=date(1996, 1, 1))
        self.assertEqual(save_record(self.record()), "created")

    def test_a_hand_entered_title_with_no_year_counts_as_a_match(self):
        CatalogItem.objects.create(title="Arrival", media_type="movie", typical_minutes=90)
        self.assertEqual(save_record(self.record()), "duplicate")

    def test_the_same_title_in_another_medium_is_not_a_duplicate(self):
        CatalogItem.objects.create(title="Arrival", media_type="book", typical_minutes=300)
        self.assertEqual(save_record(self.record()), "created")

    def test_the_database_refuses_two_rows_for_one_source_id(self):
        from django.db import IntegrityError, transaction

        CatalogItem.objects.create(title="A", media_type="movie", typical_minutes=1, source="tmdb", source_id="1")
        with self.assertRaises(IntegrityError), transaction.atomic():
            CatalogItem.objects.create(title="B", media_type="movie", typical_minutes=1, source="tmdb", source_id="1")

    def test_hand_entered_rows_have_no_such_limit(self):
        CatalogItem.objects.create(title="A", media_type="movie", typical_minutes=1)
        CatalogItem.objects.create(title="B", media_type="movie", typical_minutes=1)
        self.assertEqual(CatalogItem.objects.count(), 2)


class FakeBooks(Source):
    name, media_type, key_env = "books", MediaType.BOOK, ""

    def records(self, limit):
        yield Record("fake", "1", "First Book", "book", 300, genre="Fantasy")
        yield Record("fake", "2", "Second Book", "book", 400, genre="Fantasy")
        self.skipped["no usable page count"] += 1


class FakeMovies(Source):
    name, media_type, key_env = "movies", MediaType.MOVIE, "FAKE_MOVIE_KEY"

    def records(self, limit):
        yield Record("fake", "m1", "A Film", "movie", 100)
        raise ImportFailure("the server went away")


class ImportCommandTests(TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ, {"FAKE_MOVIE_KEY": "key"})
        patcher.start()
        self.addCleanup(patcher.stop)
        sources = mock.patch(
            "planner.management.commands.import_catalog.SOURCES", {"books": FakeBooks, "movies": FakeMovies}
        )
        sources.start()
        self.addCleanup(sources.stop)

    def run_command(self, *args):
        out, err = io.StringIO(), io.StringIO()
        call_command("import_catalog", *args, stdout=out, stderr=err)
        return out.getvalue(), err.getvalue()

    def test_imports_and_summarises(self):
        out, _ = self.run_command("--only", "books")
        self.assertEqual(CatalogItem.objects.filter(source="fake").count(), 2)
        self.assertIn("books: 2 added, left out 1 (no usable page count 1)", out)

    def test_running_twice_refreshes(self):
        self.run_command("--only", "books")
        out, _ = self.run_command("--only", "books")
        self.assertEqual(CatalogItem.objects.count(), 2)
        self.assertIn("2 refreshed", out)

    def test_dry_run_lists_titles_and_saves_nothing(self):
        out, _ = self.run_command("--only", "books", "--dry-run")
        self.assertIn("First Book (300 min)", out)
        self.assertIn("2 would add", out)
        self.assertEqual(CatalogItem.objects.count(), 0)

    def test_a_source_without_its_key_is_skipped_and_the_rest_still_run(self):
        with mock.patch.dict(os.environ):
            os.environ.pop("FAKE_MOVIE_KEY")
            out, _ = self.run_command()
        self.assertIn("Skipping movies: FAKE_MOVIE_KEY isn't set", out)
        self.assertEqual(CatalogItem.objects.filter(source="fake").count(), 2)

    def test_a_failing_source_keeps_what_it_saved_and_does_not_stop_the_others(self):
        out, err = io.StringIO(), io.StringIO()
        with self.assertRaises(CommandError) as raised:
            call_command("import_catalog", stdout=out, stderr=err)
        self.assertIn("movies", str(raised.exception))
        self.assertIn("the server went away", err.getvalue())
        self.assertEqual(sorted(CatalogItem.objects.values_list("title", flat=True)), ["A Film", "First Book", "Second Book"])

    def test_rejects_a_limit_below_one(self):
        with self.assertRaises(CommandError):
            self.run_command("--limit", "0")

    def test_passes_the_limit_to_the_source(self):
        with mock.patch.object(FakeBooks, "records", autospec=True, return_value=iter([])) as records:
            self.run_command("--only", "books", "--limit", "7")
        self.assertEqual(records.call_args[0][1], 7)


class DiscoverPagingTests(TestCase):
    def setUp(self):
        self.client.force_login(make_user())
        for number in range(60):
            CatalogItem.objects.create(
                title=f"Title {number:02d}", media_type="book" if number % 2 else "movie", typical_minutes=100 + number
            )

    def titles(self, response):
        return [item.title for item in response.context["results"]]

    def test_first_page_has_24_and_links_onward(self):
        response = self.client.get("/discover/")
        self.assertEqual(len(self.titles(response)), 24)
        self.assertContains(response, "60 results")
        self.assertContains(response, "Page 1 of 3")
        self.assertContains(response, "page=2")
        self.assertNotContains(response, ">Previous<")

    def test_last_page_has_the_rest(self):
        response = self.client.get("/discover/", {"page": 3})
        self.assertEqual(len(self.titles(response)), 12)
        self.assertContains(response, ">Previous<")
        self.assertNotContains(response, ">Next<")

    def test_pages_do_not_overlap_or_skip(self):
        seen = []
        for page in (1, 2, 3):
            seen += self.titles(self.client.get("/discover/", {"page": page}))
        self.assertEqual(seen, sorted(seen))
        self.assertEqual(len(set(seen)), 60)

    def test_filters_are_kept_in_the_page_links(self):
        response = self.client.get("/discover/", {"media_type": "book"})
        self.assertContains(response, "30 results")
        self.assertContains(response, "media_type=book&amp;page=2")

    def test_a_bad_page_number_falls_back_instead_of_failing(self):
        self.assertEqual(self.client.get("/discover/", {"page": "abc"}).status_code, 200)
        self.assertEqual(len(self.titles(self.client.get("/discover/", {"page": 999}))), 12)

    def test_a_short_result_has_no_pager(self):
        response = self.client.get("/discover/", {"q": "Title 07"})
        self.assertNotContains(response, "Pages of results")

    def test_sorting_by_length_pages_consistently(self):
        first = self.titles(self.client.get("/discover/", {"sort": "typical_minutes", "page": 1}))
        second = self.titles(self.client.get("/discover/", {"sort": "typical_minutes", "page": 2}))
        self.assertEqual(first[0], "Title 00")
        self.assertEqual(second[0], "Title 24")

    def test_every_page_credits_the_data_sources(self):
        response = self.client.get("/discover/")
        for name in ("TMDB", "RAWG", "Open Library"):
            self.assertContains(response, name)
        self.assertContains(response, "not endorsed or certified by TMDB")


# ---------------------------------------------------------------- progress


def local(year, month, day, hour=12, minute=0):
    """An aware datetime in the project's time zone."""
    return timezone.make_aware(datetime(year, month, day, hour, minute))


class AxisTests(TestCase):
    def test_round_steps_and_at_most_four_of_them(self):
        self.assertEqual(_axis(0), (15, 60))
        self.assertEqual(_axis(45), (15, 45))
        self.assertEqual(_axis(90), (30, 90))
        self.assertEqual(_axis(130), (60, 180))
        self.assertEqual(_axis(500), (180, 540))

    def test_a_huge_total_still_gets_an_axis(self):
        step, top = _axis(5000)
        self.assertEqual(step, 960)
        self.assertGreaterEqual(top, 5000)

    def test_the_axis_always_covers_the_data(self):
        for value in range(0, 3000, 37):
            with self.subTest(value=value):
                self.assertGreaterEqual(_axis(value)[1], value)


class ProgressStatsTests(TestCase):
    today = date(2026, 10, 6)  # a Tuesday

    def setUp(self):
        self.user = make_user()

    def done(self, minutes, when=None, media_type=MediaType.BOOK, user=None, **extra):
        """A session marked done at `when` (local), on a fresh backlog item."""
        user = user or self.user
        item = BacklogItem.objects.create(
            user=user, title=f"Item {BacklogItem.objects.count()}", media_type=media_type, estimated_minutes=9999
        )
        planned = when.date() if when else self.today
        return Suggestion.objects.create(
            user=user, backlog_item=item, date=extra.pop("date", planned), start_time=time(19), end_time=time(20),
            minutes=minutes, status=extra.pop("status", Suggestion.Status.DONE), responded_at=when, **extra,
        )

    def stats(self, **kwargs):
        return progress_stats(self.user, today=self.today, **kwargs)

    def test_a_new_user_has_zeros_and_an_empty_chart(self):
        stats = self.stats()
        self.assertEqual((stats["month_minutes"], stats["current_streak"], stats["best_streak"]), (0, 0, 0))
        self.assertEqual((stats["finished_total"], stats["review_count"]), (0, 0))
        self.assertIsNone(stats["average_rating"])
        self.assertFalse(stats["has_chart_data"])
        self.assertEqual(stats["recent_finished"], [])
        self.assertEqual(stats["in_progress"], [])

    def test_the_chart_covers_thirty_days_ending_today(self):
        daily = self.stats()["daily"]
        self.assertEqual(len(daily), 30)
        self.assertEqual((daily[0]["date"], daily[-1]["date"]), (self.today - timedelta(days=29), self.today))
        self.assertTrue(daily[-1]["is_today"])
        self.assertEqual(sum(1 for entry in daily if entry["is_today"]), 1)

    def test_month_total_uses_the_day_it_was_done_in_local_time(self):
        self.done(60, local(2026, 10, 1, 0, 30))    # just into October, local
        self.done(45, local(2026, 9, 30, 23, 30))   # still September here, October in UTC
        self.done(30, local(2026, 10, 6, 18))
        stats = self.stats()
        self.assertEqual((stats["month_minutes"], stats["month_sessions"]), (90, 2))
        self.assertEqual(stats["total_minutes"], 135)
        self.assertEqual(stats["month_time"], "1h 30m")

    def test_a_session_with_no_timestamp_falls_back_to_its_planned_day(self):
        self.done(50, when=None, date=date(2026, 10, 3))
        self.assertEqual(self.stats()["month_minutes"], 50)

    def test_nothing_dated_after_today_counts(self):
        self.done(50, when=None, date=date(2026, 10, 20))
        stats = self.stats()
        self.assertEqual((stats["month_minutes"], stats["total_minutes"]), (0, 0))

    def test_only_sessions_marked_done_count(self):
        for status in [Suggestion.Status.PENDING, Suggestion.Status.ACCEPTED, Suggestion.Status.REJECTED]:
            self.done(60, local(2026, 10, 6), status=status)
        self.assertEqual(self.stats()["month_minutes"], 0)

    def test_other_peoples_sessions_are_not_counted(self):
        self.done(500, local(2026, 10, 6), user=make_user("mallory"))
        self.assertEqual(self.stats()["month_minutes"], 0)

    def test_streak_and_best_streak(self):
        for day in (4, 5, 6):
            self.done(30, local(2026, 10, day))
        for day in range(20, 26):
            self.done(30, local(2026, 9, day))
        stats = self.stats()
        self.assertEqual((stats["current_streak"], stats["best_streak"]), (3, 6))

    def test_an_empty_today_does_not_end_the_streak(self):
        for day in (4, 5):
            self.done(30, local(2026, 10, day))
        self.assertEqual(self.stats()["current_streak"], 2)

    def test_a_gap_ends_the_streak(self):
        self.done(30, local(2026, 10, 3))
        self.done(30, local(2026, 10, 5))
        stats = self.stats()
        self.assertEqual((stats["current_streak"], stats["best_streak"]), (1, 1))

    def test_two_days_without_anything_means_no_current_streak(self):
        self.done(30, local(2026, 10, 4))
        stats = self.stats()
        self.assertEqual((stats["current_streak"], stats["best_streak"]), (0, 1))

    def test_several_sessions_in_a_day_are_one_streak_day_and_add_up(self):
        self.done(30, local(2026, 10, 6, 9))
        self.done(45, local(2026, 10, 6, 20))
        stats = self.stats()
        self.assertEqual(stats["current_streak"], 1)
        self.assertEqual(stats["daily"][-1]["minutes"], 75)

    def test_by_type_is_in_a_fixed_order_with_shares_of_the_month(self):
        self.done(120, local(2026, 10, 2), media_type=MediaType.MOVIE)
        self.done(60, local(2026, 10, 3), media_type=MediaType.BOOK)
        by_type = self.stats()["by_type"]
        self.assertEqual([t["key"] for t in by_type], ["movie", "tv", "book", "game"])
        self.assertEqual([t["minutes"] for t in by_type], [120, 0, 60, 0])
        self.assertEqual([t["percent"] for t in by_type], [66.7, 0, 33.3, 0])
        self.assertEqual(by_type[0]["time"], "2h")
        self.assertEqual(by_type[1]["time"], "")

    def test_by_type_ignores_last_month(self):
        self.done(120, local(2026, 9, 15), media_type=MediaType.MOVIE)
        self.assertEqual(sum(t["minutes"] for t in self.stats()["by_type"]), 0)

    def test_chart_heights_are_scaled_to_the_axis(self):
        self.done(90, local(2026, 10, 6))
        self.done(45, local(2026, 10, 5))
        stats = self.stats()
        self.assertEqual([t["label"] for t in stats["ticks"]], ["0", "30 min", "1h", "1h 30m"])
        self.assertEqual([t["bottom"] for t in stats["ticks"]], [0, 33.3, 66.7, 100])
        self.assertEqual((stats["daily"][-1]["height"], stats["daily"][-2]["height"]), (100, 50))
        self.assertTrue(stats["has_chart_data"])

    def test_best_day_is_flagged_once_and_ties_go_to_the_earlier_day(self):
        self.done(60, local(2026, 10, 1))
        self.done(60, local(2026, 10, 6))
        best = [entry for entry in self.stats()["daily"] if entry["is_best"]]
        self.assertEqual([entry["date"] for entry in best], [date(2026, 10, 1)])

    def test_the_best_day_label_stays_inside_the_card(self):
        for day, expected in [(date(2026, 9, 8), "left"), (date(2026, 9, 25), "center"), (self.today, "right")]:
            with self.subTest(day=day):
                Suggestion.objects.all().delete()
                self.done(60, local(day.year, day.month, day.day))
                best = next(entry for entry in self.stats()["daily"] if entry["is_best"])
                self.assertEqual(best["label_align"], expected)

    def test_logged_days_are_newest_first_and_skip_quiet_days(self):
        self.done(30, local(2026, 10, 1))
        self.done(30, local(2026, 10, 5))
        self.assertEqual([e["date"] for e in self.stats()["logged_days"]], [date(2026, 10, 5), date(2026, 10, 1)])

    def test_finished_counts_split_this_month_from_all_time(self):
        def finish(title, when):
            return BacklogItem.objects.create(
                user=self.user, title=title, media_type=MediaType.MOVIE, estimated_minutes=100,
                status=BacklogItem.Status.COMPLETED, completed_at=when,
            )
        finish("This month", local(2026, 10, 2))
        finish("Last month", local(2026, 9, 20))
        finish("Undated", None)
        stats = self.stats()
        self.assertEqual((stats["finished_this_month"], stats["finished_total"]), (1, 3))
        self.assertEqual([i.title for i in stats["recent_finished"]], ["This month", "Last month", "Undated"])
        self.assertEqual(stats["by_type"][0]["finished"], 1)

    def test_recently_finished_is_capped_at_five(self):
        for number in range(8):
            BacklogItem.objects.create(
                user=self.user, title=f"Done {number}", media_type=MediaType.BOOK, estimated_minutes=100,
                status=BacklogItem.Status.COMPLETED, completed_at=local(2026, 10, 1) + timedelta(hours=number),
            )
        recent = self.stats()["recent_finished"]
        self.assertEqual(len(recent), 5)
        self.assertEqual(recent[0].title, "Done 7")

    def test_average_rating(self):
        for rating in (5, 4):
            item = BacklogItem.objects.create(
                user=self.user, title=f"R{rating}", media_type=MediaType.BOOK, estimated_minutes=100
            )
            Review.objects.create(user=self.user, backlog_item=item, rating=rating)
        stats = self.stats()
        self.assertEqual((stats["average_rating"], stats["review_count"]), (4.5, 2))

    def test_in_progress_items_are_listed(self):
        BacklogItem.objects.create(
            user=self.user, title="Midway", media_type=MediaType.BOOK, estimated_minutes=100,
            minutes_completed=40, status=BacklogItem.Status.IN_PROGRESS,
        )
        BacklogItem.objects.create(user=self.user, title="Not started", media_type=MediaType.BOOK, estimated_minutes=100)
        self.assertEqual([i.title for i in self.stats()["in_progress"]], ["Midway"])


class ProgressViewTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.client.force_login(self.user)

    def log(self, minutes, user=None, media_type=MediaType.BOOK, title="Dune"):
        user = user or self.user
        item = BacklogItem.objects.create(user=user, title=title, media_type=media_type, estimated_minutes=9999)
        return Suggestion.objects.create(
            user=user, backlog_item=item, date=timezone.localdate(), start_time=time(19), end_time=time(20),
            minutes=minutes, status=Suggestion.Status.DONE, responded_at=timezone.now(),
        )

    def test_needs_login(self):
        self.client.logout()
        self.assertRedirects(self.client.get("/progress/"), "/accounts/login/?next=/progress/")

    def test_new_user_sees_empty_states_not_errors(self):
        response = self.client.get("/progress/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Nothing logged in the last 30 days")
        self.assertContains(response, "Nothing logged this month yet")
        self.assertContains(response, "No reviews yet")
        self.assertNotContains(response, "Show as a table")

    def test_the_nav_links_to_it(self):
        self.assertContains(self.client.get("/dashboard/"), 'href="/progress/"')

    def test_shows_the_numbers_the_chart_and_the_table(self):
        self.log(95)
        response = self.client.get("/progress/")
        self.assertContains(response, "1h 35m")
        self.assertContains(response, "1 session<")
        self.assertContains(response, "Show as a table")
        self.assertContains(response, 'data-tip-value="1h 35m"')
        self.assertContains(response, 'id="chart-tip"')
        self.assertContains(response, "js/stats.js")

    def test_every_media_type_is_in_the_legend_even_with_no_time(self):
        self.log(60, media_type=MediaType.GAME)
        response = self.client.get("/progress/")
        for label in ("Movie", "TV series", "Book", "Game"):
            self.assertContains(response, label)
        self.assertContains(response, "bg-viz-game")
        self.assertContains(response, "Nothing logged")

    def test_a_types_colour_is_fixed_whatever_else_is_there(self):
        self.log(60, media_type=MediaType.BOOK)
        one = self.client.get("/progress/").content.decode()
        self.log(60, media_type=MediaType.MOVIE, title="Arrival")
        two = self.client.get("/progress/").content.decode()
        self.assertEqual(one.count("bg-viz-book"), 2)   # bar segment and legend swatch
        self.assertEqual(two.count("bg-viz-book"), 2)
        self.assertGreaterEqual(two.count("bg-viz-movie"), 2)

    def test_other_users_time_is_not_shown(self):
        self.log(999, user=make_user("mallory"))
        response = self.client.get("/progress/")
        self.assertNotContains(response, "16h 39m")
        self.assertContains(response, "Nothing logged in the last 30 days")

    def test_recently_finished_links_to_a_review(self):
        item = BacklogItem.objects.create(
            user=self.user, title="Arrival", media_type=MediaType.MOVIE, estimated_minutes=116,
            status=BacklogItem.Status.COMPLETED, completed_at=timezone.now(),
        )
        self.assertContains(self.client.get("/progress/"), f"/reviews/{item.pk}/write/")

    def test_unverified_users_are_held_back_like_everywhere_else(self):
        self.client.force_login(make_user("newbie", email_verified=False))
        self.assertRedirects(self.client.get("/progress/"), "/verify/", fetch_redirect_response=False)
