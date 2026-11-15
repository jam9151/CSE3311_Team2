import importlib.util
import io
import json
import os
import re
import smtplib
import subprocess
import sys
import tempfile
import threading
import urllib.error
import urllib.parse
from dataclasses import FrozenInstanceError, replace
from dataclasses import fields as dataclass_fields
from datetime import date, datetime, time, timedelta
from email.message import Message
from pathlib import Path
from unittest import mock

from django.contrib.auth import get_user_model
from django.core import mail, signing
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import OperationalError
from django.test import Client, TestCase, override_settings
from django.urls import path
from django.utils import timezone

from mediaflow.env import clean_smtp_password, env_bool, env_int, env_list, load_env_file, parse_env_file

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
    DEFAULT_WEIGHTS,
    Interval,
    ScoringWeights,
    _axis,
    free_intervals,
    humanize_minutes,
    month_grid,
    progress_stats,
    shared_reviews,
    suggest_for_day,
    suggest_for_week,
    total_free_minutes,
    upcoming_reminders,
    walkthrough,
)
from .services import _score as score_item
from .verification import TOKEN_MAX_AGE_DAYS

# A Tuesday. Most of these tests pin a fixed date because free_intervals()
# clips gaps that have already passed when the day is today, which would make
# the results depend on when you run the suite.
TEST_DAY = date(2026, 10, 6)
# A Monday long ago. Week tests start here so no day in the window is today,
# which free_intervals trims to the current time and would make them flaky.
WEEK_START = date(2025, 3, 3)


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
        # Both gaps get something, and the 3h evening gap has room for a
        # second sitting after the first. Whatever the count, no title twice.
        self.assertGreaterEqual(len(suggestions), 2)
        self.assertEqual(len({s.backlog_item_id for s in suggestions}), len(suggestions))


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
        return suggest_for_week(self.user, start=WEEK_START, persist=False, **kwargs)

    def test_covers_seven_consecutive_days(self):
        self.add("Big book", MediaType.BOOK, 5000)
        plan = self.week()
        self.assertEqual(list(plan), [WEEK_START + timedelta(days=n) for n in range(7)])
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
        # One 90-minute evening gap, so each day has room for one sitting and
        # the variety penalty is what decides the second day.
        self.user.profile.day_start = time(20)
        self.user.profile.day_end = time(21, 30)
        self.user.profile.save()
        self.add("Book A", MediaType.BOOK, 2000)
        self.add("Book B", MediaType.BOOK, 2000)
        plan = self.week()
        titles = [plan[WEEK_START + timedelta(days=n)][0].backlog_item.title for n in range(2)]
        self.assertEqual(titles, ["Book A", "Book B"])

    def test_accepted_sessions_count_as_already_planned(self):
        item = self.add("Short book", MediaType.BOOK, 100)
        Suggestion.objects.create(
            user=self.user, backlog_item=item, date=WEEK_START + timedelta(days=3), start_time=time(19),
            end_time=time(20), minutes=100, status=Suggestion.Status.ACCEPTED,
        )
        self.assertFalse(any(self.week().values()))

    def test_days_without_free_time_are_left_empty(self):
        self.add("Big book", MediaType.BOOK, 5000)
        wednesday = WEEK_START + timedelta(days=1)
        RecurringBlock.objects.create(
            user=self.user, label="Away", weekday=wednesday.weekday(), start_time=time(8), end_time=time(23)
        )
        plan = self.week()
        self.assertEqual(plan[wednesday], [])
        self.assertTrue(plan[WEEK_START])

    def test_persist_false_writes_nothing(self):
        self.add("Big book", MediaType.BOOK, 5000)
        self.week()
        self.assertEqual(Suggestion.objects.count(), 0)

    def test_persist_true_saves_every_suggestion(self):
        self.add("Big book", MediaType.BOOK, 5000)
        plan = suggest_for_week(self.user, start=WEEK_START)
        self.assertEqual(Suggestion.objects.count(), sum(len(day) for day in plan.values()))

    def test_single_day_behaviour_is_unchanged(self):
        self.add("Big book", MediaType.BOOK, 5000)
        self.assertEqual(len(suggest_for_day(self.user, WEEK_START, persist=False)), 1)


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

    def __init__(self, routes, fallback=None):
        self.routes = routes
        self.fallback = fallback  # answers any path not in routes: fn(path, params)
        self.calls = []

    def get_json(self, url, params=None, headers=None):
        self.calls.append((url, dict(params or {}), dict(headers or {})))
        path = urllib.parse.urlparse(url).path
        if path not in self.routes:
            if self.fallback is not None:
                return self.fallback(path, dict(params or {}))
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


def tmdb_lists(kind, top, popular, discover=()):
    return {
        f"/3/{kind}/top_rated": first_page_only([{"id": i} for i in top]),
        f"/3/{kind}/popular": first_page_only([{"id": i} for i in popular]),
        f"/3/discover/{kind}": first_page_only([{"id": i} for i in discover]),
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
    {"id": 104, "name": "Detail Playtime", "genres": []},
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
    name, media_type, key_env, source_label = "books", MediaType.BOOK, "", "fake"
    last = None  # the instance the command made, so a test can look inside it

    def records(self, limit):
        FakeBooks.last = self
        for source_id, title, minutes in (("1", "First Book", 300), ("2", "Second Book", 400)):
            if source_id in self.known:
                self.already_have += 1
            else:
                yield Record("fake", source_id, title, "book", minutes, genre="Fantasy")
        self.skipped["no usable page count"] += 1


class FakeMovies(Source):
    name, media_type, key_env, source_label = "movies", MediaType.MOVIE, "FAKE_MOVIE_KEY", "fake"

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


# ---------------------------------------------------------------- scoring

# Far in the past, so free_intervals never trims a day to "right now".
SCENARIO_TUESDAY = date(2025, 3, 4)
SCENARIO_SATURDAY = date(2025, 3, 8)


def only(**weights):
    """ScoringWeights with every bonus and penalty zero except the ones given. The day counts stay."""
    counts = {"rejection_memory_days", "time_of_day_memory_days", "time_of_day_min_answers"}
    zero = {f.name: 0 for f in dataclass_fields(ScoringWeights) if f.name not in counts}
    return ScoringWeights(**{**zero, **weights})


FOUR_HOURS = Interval(time(18), time(22))


def a_book(priority=3, estimated=5000, completed=0, status=BacklogItem.Status.BACKLOG):
    return BacklogItem(
        title="A book", media_type=MediaType.BOOK, priority=priority, estimated_minutes=estimated,
        minutes_completed=completed, status=status,
    )


def a_film(priority=3, minutes=120):
    return BacklogItem(title="A film", media_type=MediaType.MOVIE, priority=priority, estimated_minutes=minutes)


def score_of(item, gap=FOUR_HOURS, rejected=0, earlier=(), weights=DEFAULT_WEIGHTS):
    """Just the score. Unsaved items have pk None, which is what the rejection lookup keys on."""
    return score_item(item, gap, {item.pk: rejected} if rejected else {}, earlier, weights)[0]


class ScoringWeightsTests(TestCase):
    """Each number does what its name says, checked with every other number set to zero."""

    def test_every_weight_at_zero_scores_nothing(self):
        self.assertEqual(score_of(a_book(priority=5), weights=only()), 0)

    def test_priority_is_worth_a_step_per_level(self):
        self.assertEqual(score_of(a_book(priority=4), weights=only(per_priority_step=7)), 28)
        self.assertEqual(score_of(a_book(priority=1), weights=only(per_priority_step=7)), 7)

    def test_a_started_item_gets_the_in_progress_bonus(self):
        started = a_book(status=BacklogItem.Status.IN_PROGRESS)
        self.assertEqual(score_of(started, weights=only(in_progress_bonus=9)), 9)
        self.assertEqual(score_of(a_book(), weights=only(in_progress_bonus=9)), 0)

    def test_gap_fill_is_scaled_by_the_share_of_the_gap_used(self):
        # A book takes 75 minutes of a 240 minute gap, a 120 minute film half of it.
        self.assertEqual(score_of(a_book(), weights=only(gap_fill_bonus=40)), 75 / 240 * 40)
        self.assertEqual(score_of(a_film(), weights=only(gap_fill_bonus=40)), 20)

    def test_only_a_film_gets_the_film_bonus(self):
        self.assertEqual(score_of(a_film(), weights=only(whole_film_bonus=11)), 11)
        self.assertEqual(score_of(a_book(), weights=only(whole_film_bonus=11)), 0)

    def test_finishing_bonus_goes_to_whatever_the_sitting_would_finish(self):
        last_hour = a_book(estimated=100, completed=40)  # 60 left, fits in one sitting
        self.assertEqual(score_of(last_hour, weights=only(finishes_item_bonus=13)), 13)
        self.assertEqual(score_of(a_book(), weights=only(finishes_item_bonus=13)), 0)
        self.assertEqual(score_of(a_film(), weights=only(finishes_item_bonus=13)), 13)

    def test_variety_penalty_is_per_earlier_sitting(self):
        weights = only(variety_penalty=6)
        self.assertEqual(score_of(a_book(), earlier=(), weights=weights), 0)
        self.assertEqual(score_of(a_book(), earlier=(75,), weights=weights), -6)
        self.assertEqual(score_of(a_book(), earlier=(75, 75, 75), weights=weights), -18)

    def test_rejection_penalty_is_per_rejection(self):
        self.assertEqual(score_of(a_book(), rejected=3, weights=only(rejection_penalty=8)), -24)

    def test_an_item_that_does_not_fit_has_no_score_at_all(self):
        self.assertIsNone(score_item(a_film(minutes=300), FOUR_HOURS, {}))

    def test_earlier_sittings_use_up_what_is_left_to_plan(self):
        # 100 minutes left, 75 already planned, so 25 left, which finishes it.
        nearly_done = a_book(estimated=100)
        _, minutes, remaining = score_item(nearly_done, FOUR_HOURS, {}, earlier=(75,))
        self.assertEqual((minutes, remaining), (25, 25))


class ScoringWeightsValidationTests(TestCase):
    def test_the_defaults_are_the_numbers_the_app_shipped_with(self):
        self.assertEqual(
            (
                DEFAULT_WEIGHTS.per_priority_step, DEFAULT_WEIGHTS.in_progress_bonus,
                DEFAULT_WEIGHTS.gap_fill_bonus, DEFAULT_WEIGHTS.whole_film_bonus,
                DEFAULT_WEIGHTS.finishes_item_bonus, DEFAULT_WEIGHTS.rejection_penalty,
                DEFAULT_WEIGHTS.variety_penalty, DEFAULT_WEIGHTS.rejection_memory_days,
            ),
            (20, 25, 30, 15, 20, 35, 15, 14),
        )

    def test_negative_numbers_are_refused_by_name(self):
        with self.assertRaisesMessage(ValueError, "rejection_penalty"):
            ScoringWeights(rejection_penalty=-1)

    def test_the_memory_window_must_be_at_least_a_day(self):
        with self.assertRaises(ValueError):
            ScoringWeights(rejection_memory_days=0)

    def test_weights_cannot_be_changed_after_the_fact(self):
        with self.assertRaises(FrozenInstanceError):
            DEFAULT_WEIGHTS.per_priority_step = 99

    def test_a_variant_is_made_with_replace_and_leaves_the_defaults_alone(self):
        gentler = replace(DEFAULT_WEIGHTS, rejection_penalty=10)
        self.assertEqual((gentler.rejection_penalty, gentler.per_priority_step), (10, 20))
        self.assertEqual(DEFAULT_WEIGHTS.rejection_penalty, 35)


class ScoringTradeoffTests(TestCase):
    """The relationships the numbers are chosen to produce, as written in ScoringWeights.

    If one of these fails after you change a weight, the comment on that weight
    is no longer true. Either the change was a mistake or the comment needs
    rewriting, and so does the README table.
    """

    w = DEFAULT_WEIGHTS

    def test_the_comments_describe_the_numbers_in_steps(self):
        step = self.w.per_priority_step
        self.assertGreater(self.w.in_progress_bonus, step)
        self.assertLess(self.w.in_progress_bonus, 2 * step)
        self.assertGreater(self.w.gap_fill_bonus, step)
        self.assertLess(self.w.gap_fill_bonus, 2 * step)
        self.assertEqual(self.w.finishes_item_bonus, step)
        self.assertGreater(self.w.rejection_penalty, step)
        self.assertLess(self.w.rejection_penalty, 2 * step)
        self.assertLess(self.w.variety_penalty, step)
        # A film's real lead is the film bonus plus the finishing bonus it always earns.
        self.assertGreater(self.w.whole_film_bonus + self.w.finishes_item_bonus, step)
        self.assertLess(self.w.whole_film_bonus + self.w.finishes_item_bonus, 2 * step)

    def test_a_started_item_beats_a_fresh_one_a_step_higher_but_not_two(self):
        started = a_book(3, status=BacklogItem.Status.IN_PROGRESS)
        self.assertGreater(score_of(started), score_of(a_book(4)))
        self.assertLess(score_of(started), score_of(a_book(5)))

    def test_one_no_drops_an_item_below_a_fresh_one_a_step_lower_but_not_two(self):
        self.assertLess(score_of(a_book(4), rejected=1), score_of(a_book(3)))
        self.assertGreater(score_of(a_book(4), rejected=1), score_of(a_book(2)))

    def test_a_second_no_drops_it_further(self):
        self.assertLess(score_of(a_book(4), rejected=2), score_of(a_book(2)))

    def test_a_repeat_costs_less_than_a_step_so_priority_buys_one_before_the_next_gets_a_turn(self):
        self.assertGreater(score_of(a_book(4), earlier=(75,)), score_of(a_book(3)))
        self.assertLess(score_of(a_book(4), earlier=(75, 75)), score_of(a_book(3)))

    def test_finishing_is_worth_exactly_a_step(self):
        last_sitting = a_book(estimated=75)   # 75 left, the sitting finishes it
        long_way_to_go = a_book(estimated=5000)  # same 75 minutes, doesn't
        self.assertEqual(score_of(last_sitting) - score_of(long_way_to_go), self.w.per_priority_step)

    def test_a_film_that_fits_beats_a_chapter_one_priority_higher(self):
        self.assertGreater(score_of(a_film(2)), score_of(a_book(3)))

    def test_a_film_is_not_rescued_by_the_bonus_when_it_is_far_less_wanted(self):
        self.assertLess(score_of(a_film(1)), score_of(a_book(4)))

    def test_a_film_can_edge_a_chapter_two_priorities_higher_in_a_gap_it_fills(self):
        # Worth knowing about: films' combined 35 point lead plus a better fill
        # beats a book two levels up. If that is more than you want, lower
        # whole_film_bonus and update this test and the README.
        self.assertGreater(score_of(a_film(2)), score_of(a_book(4)))

    def test_a_film_that_does_not_fit_is_not_in_the_running(self):
        self.assertIsNone(score_item(a_film(5, minutes=300), FOUR_HOURS, {}))


class RealisticDaysTests(TestCase):
    """The whole scheduler on a believable student's week, pinned.

    These are the outputs the app gives today and that look right. They are
    meant to change on purpose, when someone retunes the weights.
    """

    def setUp(self):
        self.user = make_user("alex")
        for label, start, end in [
            ("Lecture", time(11), time(12, 20)), ("Lecture", time(13), time(14, 20)), ("Shift", time(17), time(20)),
        ]:
            RecurringBlock.objects.create(
                user=self.user, label=label, weekday=SCENARIO_TUESDAY.weekday(), start_time=start, end_time=end
            )

    def add(self, title, media_type, total, priority, done=0, status=BacklogItem.Status.BACKLOG):
        return BacklogItem.objects.create(
            user=self.user, title=title, media_type=media_type, estimated_minutes=total, priority=priority,
            minutes_completed=done, status=status,
        )

    def student_backlog(self):
        return {
            "phm": self.add("Project Hail Mary", MediaType.BOOK, 640, 5, done=180, status=BacklogItem.Status.IN_PROGRESS),
            "dune": self.add("Dune", MediaType.MOVIE, 155, 4),
            "hades": self.add("Hades", MediaType.GAME, 1200, 3),
            "severance": self.add("Severance", MediaType.TV, 1080, 3),
            "arrival": self.add("Arrival", MediaType.MOVIE, 116, 2),
            "educated": self.add("Educated", MediaType.BOOK, 580, 2),
        }

    def plan(self, day, **kwargs):
        made = suggest_for_day(self.user, day, persist=False, **kwargs)
        return [(s.backlog_item.title, s.start_time, s.end_time, s.minutes) for s in sorted(made, key=lambda s: s.start_time)]

    def reject(self, item, days_before, day=SCENARIO_TUESDAY):
        Suggestion.objects.create(
            user=self.user, backlog_item=item, date=day - timedelta(days=days_before), start_time=time(19),
            end_time=time(20), minutes=60, status=Suggestion.Status.REJECTED,
        )

    def test_a_tuesday_between_lectures_and_a_shift(self):
        self.student_backlog()
        # Gaps: 8-11, 12:20-1, 2:20-5 and 8-11pm. The biggest three get something.
        self.assertEqual(
            self.plan(SCENARIO_TUESDAY),
            [
                ("Dune", time(8), time(10, 35), 155),              # the long morning gap fits the film
                ("Arrival", time(14, 20), time(16, 16), 116),      # the afternoon gap fits the shorter one
                ("Project Hail Mary", time(20), time(21, 15), 75),  # evening: a few chapters of the started book
            ],
        )

    def test_one_no_does_not_dethrone_a_started_priority_five_book_but_two_do(self):
        items = self.student_backlog()
        self.reject(items["phm"], 1)
        self.assertIn("Project Hail Mary", [t for t, *_ in self.plan(SCENARIO_TUESDAY)])

        self.reject(items["phm"], 2)
        titles = [t for t, *_ in self.plan(SCENARIO_TUESDAY)]
        self.assertNotIn("Project Hail Mary", titles)
        self.assertEqual(titles, ["Dune", "Severance", "Arrival"])

    def test_a_no_is_forgotten_after_the_memory_window(self):
        items = self.student_backlog()
        self.reject(items["phm"], 1)
        self.reject(items["phm"], 2)
        in_three_weeks = SCENARIO_TUESDAY + timedelta(days=21)
        self.assertIn("Project Hail Mary", [t for t, *_ in self.plan(in_three_weeks)])

    def test_a_free_saturday_gets_the_film_when_priorities_are_level(self):
        self.add("Dune", MediaType.MOVIE, 155, 3)
        self.add("Educated", MediaType.BOOK, 580, 3)
        # The film leads the day, then after a break the rest of the gap gets
        # a few chapters of the book.
        self.assertEqual(
            self.plan(SCENARIO_SATURDAY),
            [("Dune", time(8), time(10, 35), 155), ("Educated", time(10, 50), time(12, 5), 75)],
        )

    def test_a_started_priority_five_book_beats_the_film_on_a_free_saturday(self):
        self.student_backlog()
        # The started book goes first. A free day is shared out from there: a
        # break after each sitting, then the next best thing, three in all.
        self.assertEqual(
            self.plan(SCENARIO_SATURDAY),
            [
                ("Project Hail Mary", time(8), time(9, 15), 75),
                ("Dune", time(9, 30), time(12, 5), 155),
                ("Arrival", time(12, 20), time(14, 16), 116),
            ],
        )

    def test_a_week_mixes_titles_in_proportion_to_priority(self):
        # A clear week with one 90-minute evening each day, so every day is
        # one sitting and the mix across days is all the scoring.
        RecurringBlock.objects.all().delete()
        BacklogItem.objects.all().delete()
        self.user.profile.day_start = time(20)
        self.user.profile.day_end = time(21, 30)
        self.user.profile.save()
        self.add("A", MediaType.BOOK, 5000, 4)
        self.add("B", MediaType.BOOK, 5000, 3)
        plan = suggest_for_week(self.user, start=WEEK_START, persist=False)
        self.assertEqual(
            [day[0].backlog_item.title for day in plan.values()], ["A", "A", "B", "A", "B", "A", "B"]
        )


class ScoringOverrideTests(TestCase):
    """Other weights reach the real suggestions, and the defaults stay untouched."""

    def setUp(self):
        self.user = make_user()
        self.first = BacklogItem.objects.create(
            user=self.user, title="First", media_type=MediaType.BOOK, estimated_minutes=5000, priority=3
        )
        self.second = BacklogItem.objects.create(
            user=self.user, title="Second", media_type=MediaType.BOOK, estimated_minutes=5000, priority=3
        )

    def pick(self, **kwargs):
        return suggest_for_day(self.user, TEST_DAY, persist=False, **kwargs)[0].backlog_item.title

    def reject_first(self, days_before=1):
        Suggestion.objects.create(
            user=self.user, backlog_item=self.first, date=TEST_DAY - timedelta(days=days_before),
            start_time=time(19), end_time=time(20), minutes=60, status=Suggestion.Status.REJECTED,
        )

    def test_a_tie_goes_to_the_older_item(self):
        self.assertEqual(self.pick(), "First")

    def test_the_default_demotes_a_rejected_item(self):
        self.reject_first()
        self.assertEqual(self.pick(), "Second")

    def test_a_zero_rejection_penalty_ignores_the_no(self):
        self.reject_first()
        self.assertEqual(self.pick(weights=replace(DEFAULT_WEIGHTS, rejection_penalty=0)), "First")

    def test_a_shorter_memory_forgets_an_older_no(self):
        self.reject_first(days_before=5)
        self.assertEqual(self.pick(), "Second")
        self.assertEqual(self.pick(weights=replace(DEFAULT_WEIGHTS, rejection_memory_days=3)), "First")

    def test_the_week_planner_uses_the_weights_too(self):
        BacklogItem.objects.all().delete()
        BacklogItem.objects.create(user=self.user, title="A", media_type=MediaType.BOOK, estimated_minutes=5000, priority=4)
        BacklogItem.objects.create(user=self.user, title="B", media_type=MediaType.BOOK, estimated_minutes=5000, priority=3)
        plan = suggest_for_week(
            self.user, start=WEEK_START, persist=False, weights=replace(DEFAULT_WEIGHTS, variety_penalty=0)
        )
        self.assertEqual({day[0].backlog_item.title for day in plan.values()}, {"A"})


# ---------------------------------------------------------------- deploying

PROJECT_ROOT = Path(__file__).resolve().parent.parent
NO_ENV_FILE = Path(tempfile.gettempdir()) / "mediaflow-no-such-env-file"
# Long, varied and not "django-insecure-", so Django's own deploy check accepts it.
STRONG_KEY = "q7Zk!2mXp4Nw7Rt1Ys5Uc9Hd3Lf6Gj0Be8Va4Tx3Oi5Pl2Km9Jn7Hb1Gv"
PRODUCTION = {
    "DJANGO_DEBUG": "false", "DJANGO_SECRET_KEY": STRONG_KEY, "DJANGO_ALLOWED_HOSTS": "mediaflow.example.com",
    "EMAIL_BACKEND": "django.core.mail.backends.smtp.EmailBackend",  # never used, just not the console
}

DUMP_SETTINGS = """
import json
from mediaflow import settings as s
print("DUMP " + json.dumps({
    "debug": s.DEBUG, "key_is_dev": s.SECRET_KEY.startswith("django-insecure-"), "hosts": s.ALLOWED_HOSTS,
    "csrf_origins": s.CSRF_TRUSTED_ORIGINS, "validators": len(s.AUTH_PASSWORD_VALIDATORS),
    "whitenoise_second": s.MIDDLEWARE[1] == "whitenoise.middleware.WhiteNoiseMiddleware",
    "whitenoise_anywhere": any("whitenoise" in m for m in s.MIDDLEWARE),
    "cookies_secure": [getattr(s, "SESSION_COOKIE_SECURE", False), getattr(s, "CSRF_COOKIE_SECURE", False)],
    "ssl_redirect": getattr(s, "SECURE_SSL_REDIRECT", False),
    "proxy_header": getattr(s, "SECURE_PROXY_SSL_HEADER", None),
    "hsts": getattr(s, "SECURE_HSTS_SECONDS", 0),
    "static_storage": getattr(s, "STORAGES", {}).get("staticfiles", {}).get("BACKEND"),
    "static_root": str(s.STATIC_ROOT), "database": str(s.DATABASES["default"]["NAME"]),
    "log_level": s.LOGGING["root"]["level"], "silenced": getattr(s, "SILENCED_SYSTEM_CHECKS", []),
    "email_backend": s.EMAIL_BACKEND,
}))
"""

# Drives the real app in production mode the way a browser would.
PRODUCTION_SMOKE = r"""
import json, os
import django
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "mediaflow.settings")
django.setup()
from django.contrib.staticfiles.storage import staticfiles_storage
from django.core.management import call_command
from django.test import Client

call_command("migrate", verbosity=0)
browser = Client(HTTP_HOST="mediaflow.example.com")
out = {}
r = browser.get("/healthz/")
out["health_over_http"] = [r.status_code, r.json()]
r = browser.get("/")
out["home_over_http"] = [r.status_code, r["Location"] if r.status_code == 301 else None]
out["home_over_https"] = browser.get("/", secure=True).status_code
hashed = staticfiles_storage.stored_name("css/app.css")
r = browser.get("/static/" + hashed, secure=True)
out["hashed_css"] = [hashed != "css/app.css", r.status_code, "immutable" in r.get("Cache-Control", "")]
out["plain_js"] = browser.get("/static/js/stats.js", secure=True).status_code
out["tailwind_source"] = browser.get("/static/src/app.css", secure=True).status_code
out["admin_css"] = browser.get("/static/admin/css/base.css", secure=True).status_code
out["csrf_cookie_secure"] = bool(browser.get("/accounts/login/", secure=True).cookies["csrftoken"]["secure"])
r = browser.get("/nope/", secure=True)
out["not_found"] = [r.status_code, b"Page not found" in r.content, b"<h1>Not Found</h1>" in r.content]
form = {"username": "newbie", "email": "newbie@mavs.uta.edu"}
r = browser.post("/register/", {**form, "password1": "password", "password2": "password"}, secure=True)
out["weak_password"] = [r.status_code, b"too common" in r.content]
r = browser.post("/register/", {**form, "password1": "correct-horse-battery-9", "password2": "correct-horse-battery-9"}, secure=True)
out["strong_password"] = r.status_code
print("RESULT " + json.dumps(out))
"""


def run_python(args, env=None, timeout=240):
    """Run python in a fresh process whose environment is exactly what is given.

    Settings are read when the process starts, so each case needs its own. The
    developer's real .env and any DJANGO_* variables are kept out.
    """
    clean = {
        key: value for key, value in os.environ.items()
        if not key.startswith(("DJANGO_", "EMAIL_")) and key not in {"DATABASE_PATH", "DEFAULT_FROM_EMAIL"}
    }
    clean.update({"DJANGO_ENV_FILE": str(NO_ENV_FILE), "PYTHONPATH": str(PROJECT_ROOT), "PYTHONIOENCODING": "utf-8"})
    clean.update(env or {})
    return subprocess.run(
        [sys.executable, *args], cwd=PROJECT_ROOT, env=clean, capture_output=True, text=True, timeout=timeout
    )


def settings_under(env=None):
    result = run_python(["-c", DUMP_SETTINGS], env)
    marked = [line for line in result.stdout.splitlines() if line.startswith("DUMP ")]
    return result, (json.loads(marked[-1][5:]) if marked else None)


class EnvHelperTests(TestCase):
    def with_env(self, value):
        patcher = mock.patch.dict(os.environ, {} if value is None else {"MEDIAFLOW_TEST": value})
        patcher.start()
        self.addCleanup(patcher.stop)
        if value is None:
            os.environ.pop("MEDIAFLOW_TEST", None)

    def test_booleans_accept_the_usual_spellings(self):
        for raw in ("1", "true", "TRUE", " Yes ", "on"):
            with self.subTest(raw=raw), mock.patch.dict(os.environ, {"MEDIAFLOW_TEST": raw}):
                self.assertTrue(env_bool("MEDIAFLOW_TEST"))
        for raw in ("0", "false", "No", "OFF"):
            with self.subTest(raw=raw), mock.patch.dict(os.environ, {"MEDIAFLOW_TEST": raw}):
                self.assertFalse(env_bool("MEDIAFLOW_TEST", default=True))

    def test_unset_or_empty_means_the_default(self):
        self.with_env(None)
        self.assertTrue(env_bool("MEDIAFLOW_TEST", default=True))
        self.assertFalse(env_bool("MEDIAFLOW_TEST", default=False))
        with mock.patch.dict(os.environ, {"MEDIAFLOW_TEST": "  "}):
            self.assertTrue(env_bool("MEDIAFLOW_TEST", default=True))

    def test_a_typo_is_an_error_not_a_guess(self):
        with mock.patch.dict(os.environ, {"MEDIAFLOW_TEST": "flase"}):
            with self.assertRaisesMessage(ValueError, "MEDIAFLOW_TEST must be true or false"):
                env_bool("MEDIAFLOW_TEST")

    def test_integers(self):
        with mock.patch.dict(os.environ, {"MEDIAFLOW_TEST": " 3600 "}):
            self.assertEqual(env_int("MEDIAFLOW_TEST", 0), 3600)
        self.with_env(None)
        self.assertEqual(env_int("MEDIAFLOW_TEST", 587), 587)
        with mock.patch.dict(os.environ, {"MEDIAFLOW_TEST": "an hour"}):
            with self.assertRaisesMessage(ValueError, "MEDIAFLOW_TEST must be a whole number"):
                env_int("MEDIAFLOW_TEST", 0)

    def test_lists_are_trimmed_and_blanks_dropped(self):
        with mock.patch.dict(os.environ, {"MEDIAFLOW_TEST": " a.example.com, b.example.com ,, "}):
            self.assertEqual(env_list("MEDIAFLOW_TEST"), ["a.example.com", "b.example.com"])
        self.with_env(None)
        self.assertEqual(env_list("MEDIAFLOW_TEST"), [])


class SettingsTests(TestCase):
    """Settings are read once at startup, so each case runs in its own process."""

    def test_development_needs_no_configuration(self):
        result, s = settings_under()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(s["debug"])
        self.assertTrue(s["key_is_dev"])
        self.assertEqual(s["hosts"], [])
        self.assertEqual(s["validators"], 0)
        self.assertFalse(s["whitenoise_anywhere"])
        self.assertEqual(s["cookies_secure"], [False, False])
        self.assertFalse(s["ssl_redirect"])
        self.assertEqual(s["hsts"], 0)
        self.assertTrue(s["email_backend"].endswith("console.EmailBackend"))

    def test_production_will_not_start_without_a_secret_key(self):
        result, _ = settings_under({"DJANGO_DEBUG": "false", "DJANGO_ALLOWED_HOSTS": "a.example.com"})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("DJANGO_SECRET_KEY is not set", result.stderr)

    def test_production_will_not_start_on_a_placeholder_key(self):
        result, _ = settings_under({**PRODUCTION, "DJANGO_SECRET_KEY": "django-insecure-change-me"})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("placeholder", result.stderr)

    def test_production_will_not_start_without_allowed_hosts(self):
        result, _ = settings_under({"DJANGO_DEBUG": "false", "DJANGO_SECRET_KEY": STRONG_KEY})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("DJANGO_ALLOWED_HOSTS is not set", result.stderr)

    def test_a_typo_in_debug_stops_startup_instead_of_picking_a_side(self):
        result, _ = settings_under({"DJANGO_DEBUG": "flase"})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("DJANGO_DEBUG must be true or false", result.stderr)

    def test_production_defaults(self):
        result, s = settings_under(PRODUCTION)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(s["debug"])
        self.assertFalse(s["key_is_dev"])
        self.assertEqual(s["hosts"], ["mediaflow.example.com"])
        self.assertEqual(s["validators"], 4)
        self.assertTrue(s["whitenoise_second"])  # straight after SecurityMiddleware
        self.assertEqual(s["static_storage"], "whitenoise.storage.CompressedManifestStaticFilesStorage")
        self.assertEqual(s["cookies_secure"], [True, True])
        self.assertTrue(s["ssl_redirect"])
        self.assertIsNone(s["proxy_header"])  # off until a proxy is vouched for
        self.assertEqual(s["hsts"], 0)         # off until HTTPS has been seen working
        self.assertTrue(s["static_root"].endswith("staticfiles"))
        self.assertTrue(s["database"].endswith("db.sqlite3"))
        self.assertEqual(s["log_level"], "INFO")

    def test_production_options(self):
        result, s = settings_under({
            **PRODUCTION,
            "DJANGO_ALLOWED_HOSTS": "a.example.com, b.example.com",
            "DJANGO_CSRF_TRUSTED_ORIGINS": "https://a.example.com",
            "DJANGO_SSL_REDIRECT": "false", "DJANGO_BEHIND_PROXY": "true", "DJANGO_HSTS_SECONDS": "3600",
            "DJANGO_SECURE_COOKIES": "false",
            "DJANGO_LOG_LEVEL": "warning", "DATABASE_PATH": "/data/mediaflow.sqlite3",
            "DJANGO_STATIC_ROOT": "/data/static",
        })
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(s["hosts"], ["a.example.com", "b.example.com"])
        self.assertEqual(s["csrf_origins"], ["https://a.example.com"])
        self.assertFalse(s["ssl_redirect"])
        self.assertEqual(s["cookies_secure"], [False, False])
        self.assertEqual(s["proxy_header"], ["HTTP_X_FORWARDED_PROTO", "https"])
        self.assertEqual(s["hsts"], 3600)
        self.assertEqual(s["log_level"], "WARNING")
        self.assertTrue(s["database"].replace("\\", "/").endswith("/data/mediaflow.sqlite3"))
        self.assertTrue(s["static_root"].replace("\\", "/").endswith("/data/static"))

    def test_debug_can_stay_on_with_a_real_key_set(self):
        _, s = settings_under({"DJANGO_SECRET_KEY": STRONG_KEY})
        self.assertTrue(s["debug"])
        self.assertFalse(s["key_is_dev"])

    def test_settings_can_come_from_the_env_file(self):
        with tempfile.TemporaryDirectory() as folder:
            env_file = Path(folder) / ".env"
            env_file.write_text(
                f'DJANGO_DEBUG=false\nDJANGO_SECRET_KEY="{STRONG_KEY}"\nDJANGO_ALLOWED_HOSTS=from-file.example.com\n'
            )
            result, s = settings_under({"DJANGO_ENV_FILE": str(env_file)})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(s["debug"])
        self.assertEqual(s["hosts"], ["from-file.example.com"])

    def test_a_real_environment_variable_beats_the_env_file(self):
        with tempfile.TemporaryDirectory() as folder:
            env_file = Path(folder) / ".env"
            env_file.write_text("DJANGO_DEBUG=false\n")
            _, s = settings_under({"DJANGO_ENV_FILE": str(env_file), "DJANGO_DEBUG": "true"})
        self.assertTrue(s["debug"])

    def test_the_email_settings_are_untouched_by_all_this(self):
        _, s = settings_under({**PRODUCTION, "EMAIL_BACKEND": "django.core.mail.backends.smtp.EmailBackend"})
        self.assertTrue(s["email_backend"].endswith("smtp.EmailBackend"))


class DeployCheckTests(TestCase):
    """Django's own `check --deploy`, with warnings counted as failures."""

    def deploy_check(self, **extra):
        return run_python(["manage.py", "check", "--deploy", "--fail-level", "WARNING"], {**PRODUCTION, **extra})

    def test_production_settings_pass(self):
        result = self.deploy_check()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_production_settings_pass_with_hsts_on_too(self):
        result = self.deploy_check(DJANGO_HSTS_SECONDS="3600")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_console_email_in_production_is_flagged(self):
        # Without real email nobody can confirm an account, so the site looks broken.
        result = self.deploy_check(EMAIL_BACKEND="django.core.mail.backends.console.EmailBackend")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("planner.W001", result.stdout + result.stderr)
        self.assertIn("confirmation emails never reach anyone", result.stdout + result.stderr)

    def test_the_other_unreal_mail_backends_are_flagged_too(self):
        for backend in ("dummy", "locmem", "filebased"):
            with self.subTest(backend=backend):
                result = self.deploy_check(EMAIL_BACKEND=f"django.core.mail.backends.{backend}.EmailBackend")
                self.assertIn("planner.W001", result.stdout + result.stderr)

    def test_insecure_cookies_are_allowed_but_flagged(self):
        # Needed for a test server on plain http, and not something to leave on.
        result = self.deploy_check(DJANGO_SECURE_COOKIES="false")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("SESSION_COOKIE_SECURE", result.stdout + result.stderr)

    def test_the_check_really_does_catch_a_weak_setup(self):
        # Switching the HTTPS redirect off is allowed, but it is not a clean bill of health.
        result = self.deploy_check(DJANGO_SSL_REDIRECT="false")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("SECURE_SSL_REDIRECT", result.stdout + result.stderr)


class ProductionSmokeTests(TestCase):
    """Collect the static files, then use the app the way a browser would, in production mode."""

    def test_the_whole_thing_hangs_together(self):
        with tempfile.TemporaryDirectory() as folder:
            env = {
                **PRODUCTION, "DJANGO_ALLOWED_HOSTS": "mediaflow.example.com",
                "DJANGO_STATIC_ROOT": str(Path(folder) / "static"), "DATABASE_PATH": str(Path(folder) / "db.sqlite3"),
            }
            collected = run_python(["manage.py", "collectstatic", "--noinput"], env)
            self.assertEqual(collected.returncode, 0, collected.stdout + collected.stderr)
            self.assertTrue((Path(folder) / "static" / "staticfiles.json").exists())

            result = run_python(["-c", PRODUCTION_SMOKE], env)
        self.assertEqual(result.returncode, 0, result.stderr[-2000:])
        out = json.loads([line for line in result.stdout.splitlines() if line.startswith("RESULT ")][-1][7:])

        self.assertEqual(out["health_over_http"], [200, {"status": "ok"}])       # not redirected
        self.assertEqual(out["home_over_http"], [301, "https://mediaflow.example.com/"])
        self.assertEqual(out["home_over_https"], 200)
        self.assertEqual(out["hashed_css"], [True, 200, True])                   # hashed name, cached for good
        self.assertEqual((out["plain_js"], out["admin_css"]), (200, 200))
        self.assertEqual(out["tailwind_source"], 404)                            # a build input, not served
        self.assertTrue(out["csrf_cookie_secure"])
        self.assertEqual(out["not_found"], [404, True, False])                   # our page, not Django's
        self.assertEqual(out["weak_password"], [200, True])
        self.assertEqual(out["strong_password"], 302)


class HealthCheckTests(TestCase):
    def test_answers_without_signing_in(self):
        response = self.client.get("/healthz/")
        self.assertEqual((response.status_code, response.json()), (200, {"status": "ok"}))

    def test_says_so_when_the_database_is_down(self):
        with mock.patch("planner.views.connection") as connection:
            connection.cursor.side_effect = OperationalError("database is locked")
            response = self.client.get("/healthz/")
        self.assertEqual((response.status_code, response.json()), (503, {"status": "error"}))

    def test_reveals_nothing_about_the_error(self):
        with mock.patch("planner.views.connection") as connection:
            connection.cursor.side_effect = OperationalError("password=hunter2 at /srv/db")
            response = self.client.get("/healthz/")
        self.assertNotIn("hunter2", response.content.decode())


def _explode(request):
    raise RuntimeError("secret internal detail")


# Doubles as a URLconf for the 500 test below.
urlpatterns = [path("boom/", _explode)]


class ErrorPageTests(TestCase):
    def test_a_missing_page_is_a_friendly_404_with_a_way_back(self):
        response = self.client.get("/definitely/not/here/")
        self.assertEqual(response.status_code, 404)
        self.assertContains(response, "Page not found", status_code=404)
        self.assertContains(response, 'href="/"', status_code=404)

    def test_a_signed_in_student_is_sent_back_to_today(self):
        self.client.force_login(make_user())
        response = self.client.get("/definitely/not/here/")
        self.assertContains(response, "Back to Today", status_code=404)
        self.assertContains(response, 'href="/calendar/"', status_code=404)  # the usual nav is there

    def test_a_crash_shows_a_plain_page_and_none_of_the_details(self):
        with override_settings(ROOT_URLCONF="planner.tests"):
            browser = Client(raise_request_exception=False)
            with self.assertLogs("django.request", level="ERROR"):
                response = browser.get("/boom/")
        self.assertEqual(response.status_code, 500)
        self.assertContains(response, "Something went wrong", status_code=500)
        self.assertNotContains(response, "secret internal detail", status_code=500)
        self.assertNotContains(response, "stylesheet", status_code=500)  # needs nothing else to work

    def test_the_crash_is_logged_with_its_details_even_though_the_page_hides_them(self):
        with override_settings(ROOT_URLCONF="planner.tests"):
            browser = Client(raise_request_exception=False)
            with self.assertLogs("django.request", level="ERROR") as logs:
                browser.get("/boom/")
        self.assertIn("secret internal detail", "\n".join(logs.output))


# ---------------------------------------------------------------- big imports


def movie_json(item_id, runtime=100):
    return {"title": f"Movie {item_id}", "runtime": runtime, "genres": [], "credits": {"crew": []}}


class TmdbFeedTests(TestCase):
    def test_walks_top_rated_popular_and_most_voted(self):
        routes = {
            **tmdb_lists("movie", top=[1], popular=[2], discover=[3]),
            **{f"/3/movie/{i}": movie_json(i) for i in (1, 2, 3)},
        }
        fetcher = FakeFetcher(routes)
        titles = [r.title for r in MovieSource(fetcher, "abc123").records(10)]
        self.assertEqual(titles, ["Movie 1", "Movie 2", "Movie 3"])
        discover = next(params for url, params, _ in fetcher.calls if url.endswith("/discover/movie"))
        self.assertEqual(discover["sort_by"], "vote_count.desc")

    def test_tv_uses_the_same_three_feeds(self):
        fetcher = FakeFetcher(tmdb_lists("tv", top=[], popular=[], discover=[]))
        list(TvSource(fetcher, "abc123").records(10))
        self.assertEqual(
            sorted(set(fetcher.paths())), ["/3/discover/tv", "/3/tv/popular", "/3/tv/top_rated"]
        )

    def test_a_feed_running_dry_does_not_end_the_others(self):
        # TMDB's top rated TV list is far shorter than the other two.
        def popular(params):
            page = params["page"]
            return {"results": [{"id": page * 10 + k} for k in range(3)] if page <= 3 else []}

        def details(path, params):
            return movie_json(int(path.rsplit("/", 1)[1]))

        fetcher = FakeFetcher(
            {
                "/3/movie/top_rated": first_page_only([{"id": 1}]),
                "/3/movie/popular": popular,
                "/3/discover/movie": lambda params: {"results": []},
            },
            fallback=details,
        )
        titles = [r.title for r in MovieSource(fetcher, "abc123").records(100)]
        self.assertIn("Movie 30", titles)      # only on page 3 of popular
        self.assertEqual(len(titles), 10)      # 1 + 3 + 3 + 3

    def test_never_asks_for_a_page_past_500(self):
        # TMDB answers 400 beyond that. These feeds never run out, so only the cap stops it.
        def everything(path, params):
            if "/movie/" in path and path.rsplit("/", 1)[1].isdigit():
                return None  # details not found: cheap, and nothing is produced
            feed = {"top_rated": 1, "popular": 2, "movie": 3}[path.rsplit("/", 1)[1]]
            return {"results": [{"id": feed * 10_000_000 + params["page"] * 100 + k} for k in range(3)]}

        fetcher = FakeFetcher({}, fallback=everything)
        source = MovieSource(fetcher, "abc123")
        self.assertEqual(list(source.records(10**9)), [])
        pages = [params["page"] for url, params, _ in fetcher.calls if "page" in params]
        self.assertEqual((min(pages), max(pages)), (1, 500))
        self.assertEqual(len(pages), 3 * 500)
        self.assertEqual(source.skipped["details not found"], 3 * 500 * 3)

    def test_details_really_are_fetched_at_the_same_time(self):
        # Three requests wait for each other. Done one by one the barrier times out.
        barrier = threading.Barrier(3, timeout=3)

        def details(path, params):
            barrier.wait()
            return movie_json(int(path.rsplit("/", 1)[1]))

        fetcher = FakeFetcher(
            {**tmdb_lists("movie", top=[1, 2, 3], popular=[], discover=[])}, fallback=details
        )
        self.assertEqual(len(list(MovieSource(fetcher, "abc123").records(10))), 3)

    def test_running_one_at_a_time_gives_the_same_records_in_the_same_order(self):
        def build(workers):
            routes = {
                **tmdb_lists("movie", top=list(range(1, 9)), popular=list(range(9, 15)), discover=[]),
                **{f"/3/movie/{i}": movie_json(i, runtime=60 + i) for i in range(1, 15)},
            }
            source = MovieSource(FakeFetcher(routes), "abc123")
            source.workers = workers
            return [(r.source_id, r.typical_minutes) for r in source.records(100)]

        self.assertEqual(build(1), build(6))
        self.assertEqual(build(1)[0], ("movie-1", 61))

    def test_titles_already_imported_cost_no_request_and_count_toward_the_limit(self):
        routes = {
            **tmdb_lists("movie", top=[1, 2, 3, 4], popular=[], discover=[]),
            **{f"/3/movie/{i}": movie_json(i) for i in (3, 4)},
        }
        fetcher = FakeFetcher(routes)
        source = MovieSource(fetcher, "abc123", known={"movie-1", "movie-2"})
        # A limit of 3 means 3 in the catalog in all: the two it has and one more.
        self.assertEqual([r.title for r in source.records(3)], ["Movie 3"])
        self.assertEqual(source.already_have, 2)
        self.assertNotIn("/3/movie/1", fetcher.paths())
        self.assertNotIn("/3/movie/2", fetcher.paths())

    def test_nothing_is_passed_over_unless_asked(self):
        routes = {
            **tmdb_lists("movie", top=[1], popular=[], discover=[]), "/3/movie/1": movie_json(1),
        }
        source = MovieSource(FakeFetcher(routes), "abc123")
        self.assertEqual(len(list(source.records(5))), 1)
        self.assertEqual(source.already_have, 0)

    def test_tv_skips_what_it_has_too(self):
        routes = {
            **tmdb_lists("tv", top=[10, 11], popular=[], discover=[]),
            "/3/tv/11": {**TV_DETAILS[10], "name": "Eleven"},
        }
        fetcher = FakeFetcher(routes)
        source = TvSource(fetcher, "abc123", known={"tv-10"})
        self.assertEqual([r.title for r in source.records(5)], ["Eleven"])
        self.assertNotIn("/3/tv/10", fetcher.paths())
        self.assertEqual(source.already_have, 1)

    def test_a_movie_id_and_a_tv_id_are_different_things(self):
        # TMDB numbers movies and shows separately, so id 10 can be both.
        routes = {**tmdb_lists("tv", top=[10], popular=[], discover=[]), "/3/tv/10": TV_DETAILS[10]}
        source = TvSource(FakeFetcher(routes), "abc123", known={"movie-10"})
        self.assertEqual(len(list(source.records(5))), 1)
        self.assertEqual(source.already_have, 0)


class BookResumeTests(TestCase):
    def test_books_already_imported_are_passed_over_without_a_request(self):
        fetcher = FakeFetcher(book_routes())
        source = BookSource(fetcher, known={"OL1W"})
        titles = [r.title for r in source.records(28)]
        self.assertEqual(titles, ["The Magic Door"])
        self.assertEqual(source.already_have, 1)
        self.assertNotIn("/works/OL1W.json", fetcher.paths())

    def test_a_known_book_still_blocks_its_duplicate_record(self):
        # OL5W is the same book as OL1W under another id. It must not slip in.
        source = BookSource(FakeFetcher(book_routes()), known={"OL1W"})
        self.assertNotIn("dune", [r.title.lower() for r in source.records(28)])


class GameResumeTests(TestCase):
    def source(self, **kwargs):
        routes = {"/api/games": first_page_only(GAME_ROWS), **GAME_DETAILS}
        fetcher = FakeFetcher(routes)
        return GameSource(fetcher, "rawgkey", **kwargs), fetcher

    def test_a_game_already_imported_costs_no_request(self):
        source, fetcher = self.source(known={"100"})
        titles = [r.title for r in source.records(10)]
        self.assertNotIn("Hades", titles)
        self.assertNotIn("/api/games/100", fetcher.paths())
        self.assertEqual(source.already_have, 1)

    def test_a_game_the_list_says_has_no_playtime_costs_no_request(self):
        source, fetcher = self.source()
        list(source.records(10))
        self.assertNotIn("/api/games/101", fetcher.paths())  # playtime 0
        self.assertNotIn("/api/games/102", fetcher.paths())  # 900 hours

    def test_a_row_that_leaves_playtime_out_is_still_looked_up(self):
        source, fetcher = self.source()
        list(source.records(10))
        self.assertIn("/api/games/104", fetcher.paths())

    def test_the_list_is_followed_until_it_stops_answering(self):
        def pages(params):
            rows = [{"id": params["page"], "name": "Game", "playtime": 0}]
            return {"results": rows} if params["page"] <= 7 else None

        fetcher = FakeFetcher({"/api/games": pages})
        self.assertEqual(list(GameSource(fetcher, "rawgkey").records(100)), [])
        self.assertEqual(max(p["page"] for _, p, _ in fetcher.calls), 8)

    def test_it_asks_for_no_more_than_a_thousand_pages(self):
        fetcher = FakeFetcher({"/api/games": lambda p: {"results": [{"id": p["page"], "name": "G", "playtime": 0}]}})
        list(GameSource(fetcher, "rawgkey").records(10**9))
        self.assertEqual(max(p["page"] for _, p, _ in fetcher.calls), 1000)

    def test_details_are_fetched_at_the_same_time_here_too(self):
        barrier = threading.Barrier(3, timeout=3)
        rows = [{"id": i, "name": f"G{i}", "genres": []} for i in (1, 2, 3)]  # no playtime in the list

        def detail(path, params):
            barrier.wait()
            return {"playtime": 5}

        fetcher = FakeFetcher({"/api/games": first_page_only(rows)}, fallback=detail)
        self.assertEqual(len(list(GameSource(fetcher, "rawgkey").records(10))), 3)


class SkipExistingCommandTests(TestCase):
    def setUp(self):
        patcher = mock.patch(
            "planner.management.commands.import_catalog.SOURCES", {"books": FakeBooks}
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        FakeBooks.last = None

    def run_command(self, *args):
        out = io.StringIO()
        call_command("import_catalog", *args, stdout=out)
        return out.getvalue()

    def test_the_flag_hands_the_source_what_is_already_there(self):
        self.run_command("--only", "books")
        self.assertEqual(FakeBooks.last.known, set())  # nothing asked for, nothing handed over

        out = self.run_command("--only", "books", "--skip-existing")
        self.assertEqual(FakeBooks.last.known, {"1", "2"})
        self.assertIn("2 already imported, passed over", out)

    def test_only_rows_from_that_source_are_handed_over(self):
        CatalogItem.objects.create(title="By hand", media_type="book", typical_minutes=1)
        CatalogItem.objects.create(title="Elsewhere", media_type="book", typical_minutes=1, source="tmdb", source_id="x")
        CatalogItem.objects.create(title="Ours", media_type="book", typical_minutes=1, source="fake", source_id="9")
        self.run_command("--only", "books", "--skip-existing")
        self.assertEqual(FakeBooks.last.known, {"9"})

    def test_repeating_an_interrupted_run_adds_only_what_is_new(self):
        self.run_command("--only", "books")
        CatalogItem.objects.filter(source_id="2").delete()   # as if it stopped after the first
        out = self.run_command("--only", "books", "--skip-existing")
        self.assertEqual(sorted(CatalogItem.objects.values_list("source_id", flat=True)), ["1", "2"])
        self.assertIn("1 added", out)
        self.assertIn("1 already imported", out)


# ---------------------------------------------------------------- docker


def load_healthcheck():
    spec = importlib.util.spec_from_file_location("healthcheck", PROJECT_ROOT / "docker" / "healthcheck.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ContainerHealthCheckTests(TestCase):
    def setUp(self):
        self.healthcheck = load_healthcheck()

    def test_it_asks_for_the_first_real_host_name(self):
        self.assertEqual(self.healthcheck.host_header("a.example.com, b.example.com"), "a.example.com")

    def test_a_leading_dot_pattern_becomes_the_domain(self):
        self.assertEqual(self.healthcheck.host_header(".example.com"), "example.com")

    def test_a_wildcard_or_nothing_falls_back_to_localhost(self):
        self.assertEqual(self.healthcheck.host_header("*"), "localhost")
        self.assertEqual(self.healthcheck.host_header(""), "localhost")
        self.assertEqual(self.healthcheck.host_header(" , "), "localhost")

    def test_a_wildcard_is_skipped_for_a_real_name_after_it(self):
        self.assertEqual(self.healthcheck.host_header("*, real.example.com"), "real.example.com")

    def run_check(self, urlopen_result, **env):
        # One item: a response is returned, an exception is raised.
        with mock.patch.dict(os.environ, env), mock.patch("urllib.request.urlopen", side_effect=[urlopen_result]) as opened:
            return self.healthcheck.main(), opened

    def response(self, status):
        reply = mock.MagicMock()
        reply.status = status
        reply.__enter__.return_value = reply
        return reply

    def test_healthy_when_the_app_answers_200(self):
        code, opened = self.run_check(self.response(200), PORT="9000", DJANGO_ALLOWED_HOSTS="x.example.com")
        self.assertEqual(code, 0)
        request = opened.call_args[0][0]
        self.assertEqual(request.full_url, "http://127.0.0.1:9000/healthz/")
        self.assertEqual(request.get_header("Host"), "x.example.com")

    def test_unhealthy_on_a_503(self):
        error = urllib.error.HTTPError("http://127.0.0.1:8000/healthz/", 503, "down", Message(), None)
        self.assertEqual(self.run_check(error)[0], 1)

    def test_unhealthy_when_nothing_is_listening(self):
        self.assertEqual(self.run_check(urllib.error.URLError("refused"))[0], 1)

    def test_unhealthy_on_any_other_status(self):
        self.assertEqual(self.run_check(self.response(204))[0], 1)

    def test_the_port_defaults_to_8000(self):
        with mock.patch.dict(os.environ):
            os.environ.pop("PORT", None)
            _, opened = self.run_check(self.response(200))
        self.assertEqual(opened.call_args[0][0].full_url, "http://127.0.0.1:8000/healthz/")


class ContainerFilesTests(TestCase):
    """Guards on the things that would be easy to break and costly: secrets in the image."""

    def read(self, name):
        return (PROJECT_ROOT / name).read_text(encoding="utf-8")

    def test_secrets_and_the_local_database_are_kept_out_of_the_image(self):
        ignored = {line.strip() for line in self.read(".dockerignore").splitlines()}
        for name in (".env", "db.sqlite3", ".git", "venv", "node_modules"):
            self.assertIn(name, ignored)

    def test_the_app_does_not_run_as_root(self):
        dockerfile = self.read("Dockerfile")
        self.assertIn("USER app", dockerfile)
        self.assertLess(dockerfile.index("useradd"), dockerfile.index("USER app"))

    def test_the_only_secret_key_in_the_dockerfile_is_the_build_only_one(self):
        dockerfile = self.read("Dockerfile")
        self.assertEqual(dockerfile.count("DJANGO_SECRET_KEY="), 1)
        self.assertIn("build-only", dockerfile)
        self.assertNotIn("COPY .env", dockerfile)

    def test_the_container_has_a_health_check_that_points_at_our_script(self):
        self.assertIn("HEALTHCHECK", self.read("Dockerfile"))
        self.assertIn("docker/healthcheck.py", self.read("Dockerfile"))
        self.assertTrue((PROJECT_ROOT / "docker" / "healthcheck.py").exists())

    def test_compose_insists_on_a_secret_key_and_keeps_the_database_on_a_volume(self):
        compose = self.read("docker-compose.yml")
        self.assertIn("DJANGO_SECRET_KEY: ${DJANGO_SECRET_KEY:?", compose)
        self.assertIn("mediaflow-data:/data", compose)
        self.assertIn("container_name: mediaflow-web", compose)
        self.assertIn("DATABASE_PATH: /data/db.sqlite3", compose)
        self.assertIn("DJANGO_SECURE_COOKIES: ${DJANGO_SECURE_COOKIES:-true}", compose)

    def test_the_app_port_can_be_bound_to_localhost_only(self):
        self.assertIn("${BIND_ADDRESS:-0.0.0.0}:${HOST_PORT:-8000}:8000", self.read("docker-compose.yml"))

    def test_the_caddy_file_leaves_caddy_as_the_only_way_in(self):
        caddy = self.read("docker-compose.caddy.yml")
        self.assertIn("ports: !reset []", caddy)             # the app is no longer published
        self.assertIn("image: caddy:2", caddy)
        for published in ('"80:80"', '"443:443"'):
            self.assertIn(published, caddy)
        self.assertIn("./Caddyfile:/etc/caddy/Caddyfile:ro", caddy)

    def test_the_shared_proxy_layer_publishes_nothing_and_joins_the_proxys_network(self):
        proxy = self.read("docker-compose.proxy.yml")
        self.assertIn("ports: !reset []", proxy)
        self.assertIn("external: true", proxy)
        self.assertIn("- proxy", proxy)

    def test_the_shared_proxy_layer_will_not_start_without_naming_the_network(self):
        self.assertIn("${PROXY_NETWORK:?", self.read("docker-compose.proxy.yml"))

    def test_the_shared_proxy_layer_does_not_bring_its_own_proxy(self):
        # It is the alternative to the Caddy layer, not an addition to it.
        proxy = self.read("docker-compose.proxy.yml")
        self.assertNotIn("caddy:2", proxy)
        self.assertNotIn("80:80", proxy)

    def test_the_tunnel_layer_publishes_nothing_and_runs_cloudflared_beside_the_app(self):
        tunnel = self.read("docker-compose.tunnel.yml")
        self.assertIn("ports: !reset []", tunnel)
        self.assertIn("image: cloudflare/cloudflared", tunnel)
        self.assertIn("tunnel --no-autoupdate run", tunnel)
        self.assertNotIn("80:80", tunnel)
        self.assertNotIn("443:443", tunnel)

    def test_the_tunnel_layer_will_not_start_without_its_token(self):
        self.assertIn("TUNNEL_TOKEN: ${TUNNEL_TOKEN:?", self.read("docker-compose.tunnel.yml"))

    def test_no_real_tunnel_token_is_committed(self):
        # A tunnel token is a long base64 string that starts eyJ. Only a placeholder belongs here.
        for name in (".env.example", "docker-compose.tunnel.yml", "README.md"):
            self.assertNotRegex(self.read(name), r"eyJ[A-Za-z0-9+/=_-]{40,}", name)

    def test_the_tunnel_layer_is_an_alternative_not_an_addition(self):
        # The layers are chosen between in COMPOSE_FILE. None should drag in another's pieces.
        tunnel = self.read("docker-compose.tunnel.yml")
        self.assertNotIn("image: caddy", tunnel)
        self.assertNotIn("\n  caddy:", tunnel)
        self.assertNotIn("cloudflared", self.read("docker-compose.proxy.yml"))
        self.assertNotIn("cloudflared", self.read("docker-compose.caddy.yml"))

    def test_the_certificates_are_kept_on_a_volume(self):
        caddy = self.read("docker-compose.caddy.yml")
        self.assertIn("caddy-data:/data", caddy)
        self.assertIn("caddy-data:\n", caddy)

    def test_the_domain_is_set_in_one_place_and_defaults_to_ours(self):
        self.assertIn("${MEDIAFLOW_DOMAIN:-mediaflow.jessemcnary.com}", self.read("docker-compose.caddy.yml"))
        self.assertIn("{$MEDIAFLOW_DOMAIN:localhost}", self.read("Caddyfile"))

    def test_caddy_passes_requests_to_the_app_by_its_service_name(self):
        self.assertIn("reverse_proxy web:8000", self.read("Caddyfile"))

    def test_the_container_files_are_pinned_to_unix_line_endings(self):
        attributes = self.read(".gitattributes")
        for pattern in ("Dockerfile", "docker-compose*.yml", "Caddyfile", ".dockerignore"):
            self.assertIn(f"{pattern} text eol=lf", attributes)

    def test_the_readme_key_command_makes_a_key_docker_can_read(self):
        # Django's generator can emit $ and #, which compose treats as syntax in a .env file.
        readme = self.read("README.md")
        self.assertIn("secrets.token_urlsafe(50)", readme)
        self.assertNotIn("get_random_secret_key", readme)


# ---------------------------------------------------------------- iteration 3

# A Wednesday, also long past, for the overnight tests. Any date that is not
# today works; free_intervals trims today to the current time.
NIGHT_DAY = date(2025, 3, 5)


class OvernightScheduleTests(TestCase):
    """A bedtime past midnight: up by 10:00, in bed by 02:00."""

    def setUp(self):
        self.user = make_user(day_start=time(10), day_end=time(2))

    def spans(self, day=NIGHT_DAY, **kwargs):
        return [(g.start, g.end) for g in free_intervals(self.user, day, **kwargs)]

    def test_profile_knows_it_is_overnight(self):
        self.assertTrue(self.user.profile.is_overnight)
        self.assertFalse(make_user("early").profile.is_overnight)

    def test_each_date_has_the_small_hours_and_the_evening(self):
        # The gap to midnight is stored ending 23:59, the last minute there is.
        self.assertEqual(self.spans(), [(time(0), time(2)), (time(10), time(23, 59))])

    def test_the_free_time_adds_up_to_sixteen_hours(self):
        self.assertEqual(total_free_minutes(self.user, NIGHT_DAY), 16 * 60)

    def test_a_block_in_the_small_hours_splits_them(self):
        RecurringBlock.objects.create(
            user=self.user, label="Night shift", weekday=NIGHT_DAY.weekday(), start_time=time(0, 30), end_time=time(1)
        )
        self.assertEqual(self.spans(), [(time(0), time(0, 30)), (time(1), time(2)), (time(10), time(23, 59))])

    def test_a_block_that_runs_to_midnight_ends_the_evening(self):
        RecurringBlock.objects.create(
            user=self.user, label="Closing shift", weekday=NIGHT_DAY.weekday(),
            start_time=time(18), end_time=time(23, 59),
        )
        self.assertEqual(self.spans(), [(time(0), time(2)), (time(10), time(18))])

    def test_a_commitment_across_midnight_is_clipped_on_both_dates(self):
        Commitment.objects.create(
            user=self.user, title="Launch party",
            starts_at=timezone.make_aware(datetime(2025, 3, 5, 22, 0)),
            ends_at=timezone.make_aware(datetime(2025, 3, 6, 1, 0)),
        )
        self.assertEqual(self.spans(), [(time(0), time(2)), (time(10), time(22))])
        self.assertEqual(self.spans(NIGHT_DAY + timedelta(days=1)), [(time(1), time(2)), (time(10), time(23, 59))])

    def test_today_is_trimmed_to_now_in_the_small_hours(self):
        today = timezone.localdate()
        half_past_midnight = timezone.make_aware(datetime.combine(today, time(0, 30)))
        with mock.patch("django.utils.timezone.now", return_value=half_past_midnight):
            self.assertEqual(self.spans(today), [(time(0, 30), time(2)), (time(10), time(23, 59))])

    def test_today_is_trimmed_to_now_in_the_evening(self):
        today = timezone.localdate()
        evening = timezone.make_aware(datetime.combine(today, time(21, 0)))
        with mock.patch("django.utils.timezone.now", return_value=evening):
            # The small hours of today are long gone.
            self.assertEqual(self.spans(today), [(time(21), time(23, 59))])

    def test_a_late_film_is_suggested_on_the_date_it_starts(self):
        RecurringBlock.objects.create(
            user=self.user, label="Everything", weekday=NIGHT_DAY.weekday(), start_time=time(10), end_time=time(21)
        )
        film = BacklogItem.objects.create(
            user=self.user, title="Midnight film", media_type=MediaType.MOVIE, estimated_minutes=150, priority=5
        )
        (suggestion,) = suggest_for_day(self.user, NIGHT_DAY)
        self.assertEqual((suggestion.backlog_item, suggestion.date), (film, NIGHT_DAY))
        self.assertEqual((suggestion.start_time, suggestion.end_time), (time(21), time(23, 30)))

    def test_a_chapter_fits_the_small_hours(self):
        RecurringBlock.objects.create(
            user=self.user, label="Everything", weekday=NIGHT_DAY.weekday(), start_time=time(10), end_time=time(23, 59)
        )
        BacklogItem.objects.create(user=self.user, title="A book", media_type=MediaType.BOOK, estimated_minutes=600)
        (suggestion,) = suggest_for_day(self.user, NIGHT_DAY)
        # Capped at 90 and rounded down to whole 25-minute chapters.
        self.assertEqual((suggestion.start_time, suggestion.minutes), (time(0), 75))

    def test_an_accepted_late_session_is_busy_time(self):
        item = BacklogItem.objects.create(
            user=self.user, title="Series", media_type=MediaType.TV, estimated_minutes=600
        )
        Suggestion.objects.create(
            user=self.user, backlog_item=item, date=NIGHT_DAY, start_time=time(23), end_time=time(23, 59),
            minutes=60, status=Suggestion.Status.ACCEPTED,
        )
        self.assertEqual(self.spans(), [(time(0), time(2)), (time(10), time(23))])

    def test_week_planning_runs_across_overnight_days(self):
        BacklogItem.objects.create(user=self.user, title="A book", media_type=MediaType.BOOK, estimated_minutes=6000)
        plan = suggest_for_week(self.user, WEEK_START, persist=False)
        self.assertEqual(len(plan), 7)
        for day, made in plan.items():
            self.assertTrue(made, day)
            for suggestion in made:
                self.assertEqual(suggestion.date, day)

    def test_the_form_accepts_a_bedtime_after_midnight(self):
        self.client.force_login(self.user)
        response = self.client.post("/schedule/", {"save_profile": "1", "day_start": "10:00", "day_end": "02:00"})
        self.assertRedirects(response, "/schedule/")
        self.user.profile.refresh_from_db()
        self.assertEqual((self.user.profile.day_start, self.user.profile.day_end), (time(10), time(2)))

    def test_the_form_refuses_the_same_time_twice(self):
        self.client.force_login(self.user)
        response = self.client.post("/schedule/", {"save_profile": "1", "day_start": "10:00", "day_end": "10:00"})
        self.assertContains(response, "can&#x27;t be the same time")
        self.user.profile.refresh_from_db()
        self.assertEqual(self.user.profile.day_end, time(2))

    def test_the_form_explains_the_rule(self):
        self.client.force_login(self.user)
        self.assertContains(self.client.get("/schedule/"), "counts as after midnight")

    def test_the_pages_render(self):
        self.client.force_login(self.user)
        for url in ["/dashboard/", "/calendar/", "/schedule/", f"/calendar/?date={NIGHT_DAY}"]:
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 200)

    def test_a_normal_day_is_unchanged(self):
        self.assertEqual(
            [(g.start, g.end) for g in free_intervals(make_user("early"), NIGHT_DAY)], [(time(8), time(23))]
        )


class PasswordResetTests(TestCase):
    link_pattern = re.compile(r"http://testserver(/accounts/reset/\S+/\S+/)")

    def setUp(self):
        self.user = make_user("forgetful")

    def request_reset(self, email="forgetful@mavs.uta.edu"):
        return self.client.post("/accounts/password_reset/", {"email": email})

    def link(self):
        return self.link_pattern.search(mail.outbox[-1].body).group(1)

    def test_sign_in_page_links_to_it(self):
        self.assertContains(self.client.get("/accounts/login/"), "/accounts/password_reset/")

    def test_the_form_uses_this_apps_pages_not_the_admins(self):
        response = self.client.get("/accounts/password_reset/")
        self.assertContains(response, "Forgot your password?")
        self.assertTemplateUsed(response, "base.html")

    def test_asking_sends_one_email_with_a_link(self):
        self.assertRedirects(self.request_reset(), "/accounts/password_reset/done/")
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["forgetful@mavs.uta.edu"])
        self.assertEqual(mail.outbox[0].subject, "Reset your MediaFlow password")
        self.assertRegex(mail.outbox[0].body, self.link_pattern)

    def test_the_address_is_matched_regardless_of_case(self):
        self.request_reset("Forgetful@MAVS.uta.edu")
        self.assertEqual(len(mail.outbox), 1)

    def test_an_unknown_address_gets_the_same_page_and_no_email(self):
        # Saying "no such account" would tell a stranger which addresses are registered.
        self.assertRedirects(self.request_reset("nobody@mavs.uta.edu"), "/accounts/password_reset/done/")
        self.assertEqual(mail.outbox, [])

    def test_the_done_page_mentions_the_terminal_when_mail_is_printed_there(self):
        with override_settings(EMAIL_BACKEND="django.core.mail.backends.console.EmailBackend"):
            self.assertContains(self.client.get("/accounts/password_reset/done/"), "printed in the terminal")
        with override_settings(EMAIL_BACKEND="django.core.mail.backends.smtp.EmailBackend"):
            self.assertNotContains(self.client.get("/accounts/password_reset/done/"), "printed in the terminal")

    def test_the_link_sets_a_new_password_and_signs_in(self):
        self.request_reset()
        # Django's confirm view first redirects to a URL with the token in the session.
        response = self.client.get(self.link(), follow=True)
        self.assertContains(response, "Choose a new password")
        response = self.client.post(
            response.redirect_chain[-1][0],
            {"new_password1": "fresh-passphrase-9", "new_password2": "fresh-passphrase-9"},
        )
        self.assertRedirects(response, "/accounts/reset/done/")
        self.assertIn("_auth_user_id", self.client.session)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("fresh-passphrase-9"))
        self.assertFalse(self.user.check_password("test-password"))

    def test_mismatched_passwords_are_refused(self):
        self.request_reset()
        response = self.client.get(self.link(), follow=True)
        response = self.client.post(
            response.redirect_chain[-1][0], {"new_password1": "one-thing", "new_password2": "another"}
        )
        self.assertEqual(response.status_code, 200)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("test-password"))

    def test_the_link_works_once(self):
        self.request_reset()
        link = self.link()
        response = self.client.get(link, follow=True)
        self.client.post(
            response.redirect_chain[-1][0],
            {"new_password1": "fresh-passphrase-9", "new_password2": "fresh-passphrase-9"},
        )
        self.client.post("/accounts/logout/")
        self.assertContains(self.client.get(link, follow=True), "That link didn't work")

    def test_a_forged_link_is_refused(self):
        response = self.client.get("/accounts/reset/MQ/not-a-real-token/", follow=True)
        self.assertContains(response, "That link didn't work")
        self.assertContains(response, "/accounts/password_reset/")

    def test_a_successful_reset_also_confirms_the_email(self):
        # Opening a link sent to the address proves it is theirs, same as the sign-up link.
        unverified = make_user("pending", email_verified=False)
        self.request_reset("pending@mavs.uta.edu")
        response = self.client.get(self.link(), follow=True)
        self.client.post(
            response.redirect_chain[-1][0],
            {"new_password1": "fresh-passphrase-9", "new_password2": "fresh-passphrase-9"},
        )
        unverified.profile.refresh_from_db()
        self.assertTrue(unverified.profile.email_verified)
        self.assertEqual(self.client.get("/dashboard/").status_code, 200)

    def test_an_unverified_signed_in_user_can_still_reach_the_reset_pages(self):
        self.client.force_login(make_user("stuck", email_verified=False))
        self.assertEqual(self.client.get("/accounts/password_reset/").status_code, 200)
        self.assertRedirects(self.client.get("/dashboard/"), "/verify/", fetch_redirect_response=False)

    def test_a_mail_server_that_is_down_is_a_message_not_a_crash(self):
        with mock.patch(
            "django.contrib.auth.forms.PasswordResetForm.send_mail", side_effect=smtplib.SMTPException("down")
        ):
            response = self.client.post("/accounts/password_reset/", {"email": "forgetful@mavs.uta.edu"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "couldn&#x27;t send the email right now")

    def test_signed_in_users_can_change_their_password(self):
        self.client.force_login(self.user)
        # Reached through Settings, which the navigation links on every page.
        self.assertContains(self.client.get("/dashboard/"), 'href="/settings/"')
        self.assertContains(self.client.get("/settings/"), "/accounts/password_change/")
        response = self.client.post(
            "/accounts/password_change/",
            {"old_password": "test-password", "new_password1": "newer-passphrase-2", "new_password2": "newer-passphrase-2"},
        )
        self.assertRedirects(response, "/accounts/password_change/done/")
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("newer-passphrase-2"))

    def test_changing_needs_the_old_password(self):
        self.client.force_login(self.user)
        response = self.client.post(
            "/accounts/password_change/",
            {"old_password": "wrong", "new_password1": "newer-passphrase-2", "new_password2": "newer-passphrase-2"},
        )
        self.assertEqual(response.status_code, 200)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("test-password"))

    def test_changing_needs_a_sign_in(self):
        self.assertRedirects(
            self.client.get("/accounts/password_change/"), "/accounts/login/?next=/accounts/password_change/"
        )


class WalkthroughTests(TestCase):
    def setUp(self):
        self.user = make_user("newcomer")
        self.client.force_login(self.user)

    def steps(self):
        return walkthrough(self.user)

    def test_a_new_account_starts_at_zero_with_the_schedule_step_first(self):
        result = self.steps()
        self.assertEqual((result["done"], result["total"]), (0, 5))
        self.assertEqual(result["next"]["key"], "schedule")
        self.assertEqual([s["current"] for s in result["steps"]], [True, False, False, False, False])

    def test_steps_are_credited_from_the_data(self):
        RecurringBlock.objects.create(
            user=self.user, label="Class", weekday=0, start_time=time(9), end_time=time(10)
        )
        item = BacklogItem.objects.create(
            user=self.user, title="Dune", media_type=MediaType.MOVIE, estimated_minutes=155
        )
        self.assertEqual(self.steps()["done"], 2)
        self.assertEqual(self.steps()["next"]["key"], "suggest")

        suggestion = Suggestion.objects.create(
            user=self.user, backlog_item=item, date=TEST_DAY, start_time=time(19), end_time=time(21, 35), minutes=155
        )
        self.assertEqual(self.steps()["next"]["key"], "accept")
        suggestion.status = Suggestion.Status.ACCEPTED
        suggestion.save()
        self.assertEqual(self.steps()["next"]["key"], "done")
        suggestion.status = Suggestion.Status.DONE
        suggestion.save()
        self.assertIsNone(self.steps())

    def test_a_commitment_counts_as_a_schedule(self):
        Commitment.objects.create(
            user=self.user, title="Meeting",
            starts_at=timezone.make_aware(datetime(2030, 1, 1, 9)), ends_at=timezone.make_aware(datetime(2030, 1, 1, 10)),
        )
        self.assertTrue(self.steps()["steps"][0]["done"])

    def test_a_skipped_step_is_still_credited(self):
        # Someone who adds a backlog item before a schedule is 1 of 5 done, with
        # the schedule step still the one to do.
        BacklogItem.objects.create(user=self.user, title="Dune", media_type=MediaType.MOVIE, estimated_minutes=155)
        result = self.steps()
        self.assertEqual(result["done"], 1)
        self.assertEqual(result["next"]["key"], "schedule")

    def test_today_shows_it_with_a_link_to_the_next_step(self):
        response = self.client.get("/dashboard/")
        self.assertContains(response, "Getting started")
        self.assertContains(response, "0 of 5 done")
        self.assertContains(response, "/schedule/")
        # The checklist replaces the old one-line banner while it is up.
        self.assertNotContains(response, "We can't find your free time yet")

    def test_hiding_it(self):
        response = self.client.post("/dashboard/walkthrough/hide/")
        self.assertRedirects(response, "/dashboard/")
        self.user.profile.refresh_from_db()
        self.assertTrue(self.user.profile.walkthrough_dismissed)
        self.assertIsNone(self.steps())
        page = self.client.get("/dashboard/")
        self.assertNotContains(page, "Getting started")
        # With it hidden and still no schedule, the short banner comes back.
        self.assertContains(page, "We can't find your free time yet")

    def test_hiding_needs_a_post(self):
        self.assertEqual(self.client.get("/dashboard/walkthrough/hide/").status_code, 405)

    def test_hiding_needs_a_sign_in(self):
        self.client.post("/accounts/logout/")
        self.assertRedirects(
            self.client.post("/dashboard/walkthrough/hide/"),
            "/accounts/login/?next=/dashboard/walkthrough/hide/",
        )

    def test_one_users_progress_is_not_anothers(self):
        other = make_user("other")
        BacklogItem.objects.create(user=other, title="Dune", media_type=MediaType.MOVIE, estimated_minutes=155)
        self.assertEqual(self.steps()["done"], 0)

    def test_the_demo_account_sees_it_too(self):
        call_command("seed_catalog", "--demo", stdout=io.StringIO())
        demo = get_user_model().objects.get(username="demo")
        self.assertIsNotNone(walkthrough(demo))


class RichReviewTests(TestCase):
    def setUp(self):
        self.user = make_user("critic")
        self.client.force_login(self.user)
        self.catalog = CatalogItem.objects.create(
            title="Arrival", media_type=MediaType.MOVIE, typical_minutes=116, creator="Denis Villeneuve"
        )
        self.item = BacklogItem.objects.create(
            user=self.user, title="Arrival", media_type=MediaType.MOVIE, estimated_minutes=116,
            catalog_item=self.catalog, status=BacklogItem.Status.COMPLETED, completed_at=timezone.now(),
        )

    def post_review(self, **overrides):
        data = {
            "rating": 4, "recommend": "yes", "headline": "Quiet and clever", "body": "Worth a slow evening.",
            "shared": "on",
        }
        data.update(overrides)
        return self.client.post(f"/reviews/{self.item.pk}/write/", data)

    def test_the_form_asks_the_apps_question(self):
        response = self.client.get(f"/reviews/{self.item.pk}/write/")
        self.assertContains(response, "Was it worth the time?")
        self.assertContains(response, "star-picker")
        self.assertNotContains(response, 'name="rating" value=""')
        self.assertContains(response, 'name="rating" value="5"')
        self.assertContains(response, "Share with other students")

    def test_saving_keeps_everything(self):
        self.assertRedirects(self.post_review(contains_spoilers="on"), "/reviews/")
        review = Review.objects.get(backlog_item=self.item)
        self.assertEqual((review.rating, review.recommend, review.headline), (4, True, "Quiet and clever"))
        self.assertTrue(review.contains_spoilers)
        self.assertTrue(review.shared)
        self.assertEqual(review.verdict, "Worth the time")

    def test_not_worth_it_and_private(self):
        self.post_review(recommend="no", shared="")
        review = Review.objects.get(backlog_item=self.item)
        self.assertFalse(review.recommend)
        self.assertFalse(review.shared)
        self.assertEqual(review.verdict, "Not worth the time")

    def test_the_verdict_is_required(self):
        response = self.post_review(recommend="")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Review.objects.exists())

    def test_editing_shows_the_saved_verdict(self):
        self.post_review(recommend="no")
        response = self.client.get(f"/reviews/{self.item.pk}/write/")
        self.assertContains(response, 'id="id_recommend_1" required checked')

    def test_next_sends_you_back_where_you_started(self):
        url = self.catalog.get_absolute_url()
        response = self.client.post(f"/reviews/{self.item.pk}/write/", {
            "rating": 5, "recommend": "yes", "next": url,
        })
        self.assertRedirects(response, url)

    def test_reviews_page_summarises_and_filters(self):
        self.post_review()
        book = BacklogItem.objects.create(
            user=self.user, title="Dune", media_type=MediaType.BOOK, estimated_minutes=900,
            status=BacklogItem.Status.COMPLETED,
        )
        Review.objects.create(user=self.user, backlog_item=book, rating=2, recommend=False, headline="Dragged")

        page = self.client.get("/reviews/")
        self.assertContains(page, "Quiet and clever")
        self.assertContains(page, "Dragged")
        self.assertContains(page, "3.0")  # average of 4 and 2
        self.assertContains(page, "50%")  # one yes, one no

        books = self.client.get("/reviews/?type=book")
        self.assertContains(books, "Dragged")
        self.assertNotContains(books, "Quiet and clever")

        lowest = self.client.get("/reviews/?sort=lowest")
        self.assertLess(lowest.content.index(b"Dragged"), lowest.content.index(b"Quiet and clever"))
        highest = self.client.get("/reviews/?sort=highest")
        self.assertLess(highest.content.index(b"Quiet and clever"), highest.content.index(b"Dragged"))

    def test_bad_filter_values_fall_back(self):
        self.post_review()
        self.assertContains(self.client.get("/reviews/?type=vinyl&sort=sideways"), "Quiet and clever")

    def test_spoilers_are_folded_away(self):
        self.post_review(contains_spoilers="on")
        page = self.client.get("/reviews/")
        self.assertContains(page, "<details")
        self.assertContains(page, "Show the review (spoilers)")

    def test_catalog_page_shows_other_students_shared_reviews_only(self):
        other = make_user("other")
        theirs = BacklogItem.objects.create(
            user=other, title="Arrival", media_type=MediaType.MOVIE, estimated_minutes=116,
            catalog_item=self.catalog, status=BacklogItem.Status.COMPLETED,
        )
        Review.objects.create(user=other, backlog_item=theirs, rating=5, recommend=True, headline="Loved it")
        private = make_user("private")
        private_item = BacklogItem.objects.create(
            user=private, title="Arrival", media_type=MediaType.MOVIE, estimated_minutes=116,
            catalog_item=self.catalog, status=BacklogItem.Status.COMPLETED,
        )
        Review.objects.create(
            user=private, backlog_item=private_item, rating=1, recommend=False, headline="Kept to myself", shared=False
        )
        self.post_review(headline="My own take")

        page = self.client.get(self.catalog.get_absolute_url())
        self.assertContains(page, "Loved it")
        self.assertContains(page, "other")  # the reviewer's username
        self.assertNotContains(page, "Kept to myself")
        # Your own review sits in its own section, not among the others.
        self.assertContains(page, "Your review")
        self.assertContains(page, "My own take")
        self.assertContains(page, "5.0/5 from 1 review")
        self.assertContains(page, "100% say worth it")

    def test_catalog_page_with_no_reviews(self):
        self.assertContains(self.client.get(self.catalog.get_absolute_url()), "No shared reviews of this yet")

    def test_catalog_page_prompts_for_a_review_once_finished(self):
        self.assertContains(self.client.get(self.catalog.get_absolute_url()), "Say whether it was worth the time")

    def test_the_summary_helper(self):
        other = make_user("other")
        for rating, recommend in [(5, True), (3, False), (4, None)]:
            item = BacklogItem.objects.create(
                user=other, title=f"copy {rating}", media_type=MediaType.MOVIE, estimated_minutes=1,
                catalog_item=self.catalog if rating == 5 else None,
            )
            Review.objects.create(user=other, backlog_item=item, rating=rating, recommend=recommend)
        summary = shared_reviews(self.catalog)
        # Only the review attached to this catalog title counts.
        self.assertEqual((summary["count"], summary["average"], summary["recommend_percent"]), (1, 5.0, 100))
        self.assertEqual(shared_reviews(self.catalog, exclude_user=other)["count"], 0)

    def test_old_reviews_without_a_verdict_still_render(self):
        Review.objects.create(user=self.user, backlog_item=self.item, rating=3)
        self.assertContains(self.client.get("/reviews/"), "3/5")

    def test_cannot_review_someone_elses_item(self):
        other = make_user("other")
        theirs = BacklogItem.objects.create(user=other, title="Theirs", media_type=MediaType.BOOK, estimated_minutes=1)
        self.assertEqual(self.client.get(f"/reviews/{theirs.pk}/write/").status_code, 404)


class PriorityFormatTests(TestCase):
    """One way to write a priority everywhere: P4 and its label."""

    def setUp(self):
        self.user = make_user("tidy")
        self.client.force_login(self.user)
        self.catalog = CatalogItem.objects.create(title="Dune", media_type=MediaType.MOVIE, typical_minutes=155)
        self.item = BacklogItem.objects.create(
            user=self.user, title="Dune", media_type=MediaType.MOVIE, estimated_minutes=155, priority=4,
            catalog_item=self.catalog,
        )

    def test_the_property(self):
        self.assertEqual(self.item.priority_display, "P4 · High")
        self.assertEqual(self.item.get_priority_display(), "P4 · High")

    def test_every_page_uses_it(self):
        Suggestion.objects.create(
            user=self.user, backlog_item=self.item, date=timezone.localdate(),
            start_time=time(19), end_time=time(21, 35), minutes=155,
        )
        for url in ["/dashboard/", "/backlog/", self.catalog.get_absolute_url()]:
            with self.subTest(url=url):
                page = self.client.get(url)
                self.assertContains(page, "P4 · High")
                self.assertNotContains(page, "Priority 4")
                self.assertNotContains(page, "priority 4")

    def test_the_backlog_dropdown_and_the_form_list_the_labels(self):
        dropdown = self.client.get("/backlog/")
        self.assertContains(dropdown, "P5 · Must play next")
        self.assertContains(dropdown, "P1 · Someday")
        form = self.client.get(f"/backlog/{self.item.pk}/edit/")
        self.assertContains(form, '<option value="4" selected>P4 · High</option>', html=True)


class ColdStartFlowTests(TestCase):
    """The cold-start user test, automated: sign up, verify, set up a week,
    accept a suggestion, mark it done, review it, and the checklist goes away.
    docs/iteration3-cold-start-test.md is the same run with real people."""

    link_pattern = re.compile(r"http://testserver(/verify/\S+)")

    def test_the_whole_first_session(self):
        CatalogItem.objects.create(title="Arrival", media_type=MediaType.MOVIE, typical_minutes=116)

        # Sign up, land on the holding page, open the link from the email.
        response = self.client.post("/register/", {
            "username": "fresh", "email": "fresh@mavs.uta.edu",
            "password1": "sturdy-passphrase-1", "password2": "sturdy-passphrase-1",
        })
        self.assertRedirects(response, "/verify/")
        self.assertRedirects(self.client.get("/dashboard/"), "/verify/", fetch_redirect_response=False)
        link = self.link_pattern.search(mail.outbox[0].body).group(1)
        self.assertRedirects(self.client.get(link), "/schedule/")

        # Today shows the checklist at zero.
        self.assertContains(self.client.get("/dashboard/"), "0 of 5 done")

        # Waking hours, past midnight, and one class.
        self.client.post("/schedule/", {"save_profile": "1", "day_start": "09:00", "day_end": "01:00"})
        self.client.post("/schedule/", {
            "add_block": "1", "label": "CSE 3311", "kind": "class", "weekday": TEST_DAY.weekday(),
            "start_time": "11:00", "end_time": "12:20",
        })
        self.assertContains(self.client.get("/dashboard/"), "1 of 5 done")

        # Something to watch, from Discover.
        arrival = CatalogItem.objects.get(title="Arrival")
        self.client.post(f"/media/{arrival.pk}/add/")
        self.assertContains(self.client.get("/dashboard/"), "2 of 5 done")

        # A suggestion for a day that is all ahead of us.
        self.client.post("/suggestions/refresh/", {"date": str(TEST_DAY), "next": f"/calendar/?date={TEST_DAY}"})
        suggestion = Suggestion.objects.get(user__username="fresh")
        self.assertEqual(suggestion.backlog_item.title, "Arrival")
        self.assertContains(self.client.get("/dashboard/"), "3 of 5 done")

        # Onto the calendar, then done.
        self.client.post(f"/suggestions/{suggestion.pk}/accept/")
        self.assertContains(self.client.get("/dashboard/"), "4 of 5 done")
        self.client.post(f"/suggestions/{suggestion.pk}/done/")
        dashboard = self.client.get("/dashboard/")
        self.assertNotContains(dashboard, "Getting started")

        # It is finished, so it is waiting for a review, and the time is on Progress.
        item = BacklogItem.objects.get(user__username="fresh")
        self.assertEqual(item.status, BacklogItem.Status.COMPLETED)
        self.assertContains(self.client.get("/reviews/"), "Finished, not reviewed yet")
        self.client.post(f"/reviews/{item.pk}/write/", {"rating": 5, "recommend": "yes", "headline": "Great"})
        self.assertContains(self.client.get("/reviews/"), "Worth the time")
        self.assertContains(self.client.get("/progress/"), "1h 56m")

        # And they can get back in if they forget the password.
        self.client.post("/accounts/logout/")
        self.client.post("/accounts/password_reset/", {"email": "fresh@mavs.uta.edu"})
        self.assertEqual(len(mail.outbox), 2)


# ------------------------------------------------------------ iteration 4

from .models import DASHBOARD_PANEL_KEYS, TimeLog  # noqa: E402
from .services import Interval, _commitments_by_day, _remainder_gap, month_grid  # noqa: E402


class DashboardPanelTests(TestCase):
    """Today's panels can be hidden one at a time or picked in the customizer."""

    def setUp(self):
        self.user = make_user()
        self.client.force_login(self.user)

    def hidden(self):
        self.user.profile.refresh_from_db()
        return self.user.profile.hidden_panels

    def test_every_panel_shows_by_default(self):
        page = self.client.get("/dashboard/")
        for key in ("numbers", "suggestions", "planned", "day", "backlog"):
            self.assertContains(page, f'data-panel="{key}"')
        self.assertContains(page, "Customize this page")

    def test_hide_button_removes_one_panel(self):
        response = self.client.post("/dashboard/panels/", {"hide": "planned"})
        self.assertRedirects(response, "/dashboard/")
        self.assertEqual(self.hidden(), "planned")
        page = self.client.get("/dashboard/")
        self.assertNotContains(page, 'data-panel="planned"')
        self.assertContains(page, 'data-panel="suggestions"')
        self.assertContains(page, "1 panel hidden")

    def test_show_brings_it_back(self):
        self.client.post("/dashboard/panels/", {"hide": "planned"})
        self.client.post("/dashboard/panels/", {"show": "planned"})
        self.assertEqual(self.hidden(), "")

    def test_customizer_keeps_only_the_ticked_panels_in_page_order(self):
        self.client.post("/dashboard/panels/", {"panels": ["day", "suggestions"]})
        self.assertEqual(self.hidden(), "numbers,planned,followup,invites,backlog,upcoming")
        page = self.client.get("/dashboard/")
        self.assertNotContains(page, 'data-panel="numbers"')
        self.assertContains(page, 'data-panel="day"')

    def test_unknown_keys_are_ignored(self):
        self.client.post("/dashboard/panels/", {"hide": "footer"})
        self.assertEqual(self.hidden(), "")
        self.user.profile.hidden_panels = "planned,bogus"
        self.assertEqual(self.user.profile.hidden_panel_keys, {"planned"})

    def test_hiding_everything_still_leaves_a_way_back(self):
        self.client.post("/dashboard/panels/", {"panels": []})
        self.assertEqual(self.hidden(), ",".join(DASHBOARD_PANEL_KEYS))
        page = self.client.get("/dashboard/")
        self.assertContains(page, "Every panel is hidden")
        self.assertContains(page, "Suggest something")
        self.assertContains(page, 'name="panels"')

    def test_panels_need_a_post(self):
        self.assertEqual(self.client.get("/dashboard/panels/").status_code, 405)


class CalendarGridTests(TestCase):
    """Commitments and busy time show in the month grid, not only planned sittings."""

    def setUp(self):
        self.user = make_user()
        self.client.force_login(self.user)

    def commit(self, title, start, end):
        return Commitment.objects.create(
            user=self.user, title=title, starts_at=timezone.make_aware(start), ends_at=timezone.make_aware(end)
        )

    def cells(self, year=2026, month=10):
        return {cell["date"]: cell for week in month_grid(self.user, year, month) for cell in week}

    def test_commitment_is_listed_in_its_day_cell(self):
        self.commit("Dentist", datetime(2026, 10, 6, 14), datetime(2026, 10, 6, 16))
        page = self.client.get("/calendar/?year=2026&month=10&date=2026-10-06")
        self.assertContains(page, 'data-entry="commitment">Dentist</span>')

    def test_busy_label_sums_recurring_blocks_and_commitments(self):
        RecurringBlock.objects.create(
            user=self.user, label="Lecture", weekday=TEST_DAY.weekday(), start_time=time(9), end_time=time(11)
        )
        self.commit("Dentist", datetime(2026, 10, 6, 14), datetime(2026, 10, 6, 16))
        cell = self.cells()[TEST_DAY]
        self.assertEqual(cell["busy_minutes"], 240)
        self.assertEqual(cell["busy_label"], "4h")
        self.assertEqual(cell["recurring_count"], 1)
        # The other Tuesdays only have the lecture.
        self.assertEqual(self.cells()[TEST_DAY + timedelta(days=7)]["busy_label"], "2h")
        self.assertContains(self.client.get("/calendar/?year=2026&month=10"), "4h busy")

    def test_commitments_come_before_planned_sittings_in_the_cell(self):
        item = BacklogItem.objects.create(user=self.user, title="Dune", media_type=MediaType.MOVIE, estimated_minutes=155)
        Suggestion.objects.create(
            user=self.user, backlog_item=item, date=TEST_DAY, start_time=time(19), end_time=time(21, 35),
            minutes=155, status=Suggestion.Status.ACCEPTED,
        )
        self.commit("Dentist", datetime(2026, 10, 6, 14), datetime(2026, 10, 6, 16))
        entries = self.cells()[TEST_DAY]["entries"]
        self.assertEqual([(e["kind"], e["title"]) for e in entries], [("commitment", "Dentist"), ("planned", "Dune")])

    def test_a_commitment_over_midnight_is_on_both_days_with_clipped_minutes(self):
        self.commit("Road trip", datetime(2026, 10, 6, 22), datetime(2026, 10, 7, 1))
        by_day = _commitments_by_day(self.user, date(2026, 10, 1), 31)
        self.assertEqual([m for _, m in by_day[date(2026, 10, 6)]], [120])
        self.assertEqual([m for _, m in by_day[date(2026, 10, 7)]], [60])
        self.assertNotIn(date(2026, 10, 8), by_day)

    def test_a_commitment_ending_at_midnight_does_not_spill_into_the_next_day(self):
        self.commit("Party", datetime(2026, 10, 6, 20), datetime(2026, 10, 7, 0))
        by_day = _commitments_by_day(self.user, date(2026, 10, 1), 31)
        self.assertIn(date(2026, 10, 6), by_day)
        self.assertNotIn(date(2026, 10, 7), by_day)

    def test_empty_day_has_no_busy_label(self):
        self.assertEqual(self.cells()[TEST_DAY]["busy_label"], "")

    def test_day_panel_offers_to_plan_or_add_a_commitment(self):
        page = self.client.get("/calendar/?date=2026-10-06")
        self.assertContains(page, "/calendar/plan/?date=2026-10-06")
        self.assertContains(page, "/schedule/commitment/new/?date=2026-10-06")


class PlanSessionTests(TestCase):
    """Putting a backlog item on a day yourself from the calendar."""

    def setUp(self):
        self.user = make_user()
        self.client.force_login(self.user)
        # Busy until 19:00 on the test Tuesday, so the one gap is 19:00-23:00.
        RecurringBlock.objects.create(
            user=self.user, label="Work", weekday=TEST_DAY.weekday(), start_time=time(8), end_time=time(19)
        )
        self.item = BacklogItem.objects.create(
            user=self.user, title="A book", media_type=MediaType.BOOK, estimated_minutes=600
        )
        self.url = "/calendar/plan/?date=2026-10-06"

    def test_form_starts_in_the_first_free_gap(self):
        page = self.client.get(self.url)
        self.assertContains(page, "Plan something for Tuesday, October 6")
        self.assertContains(page, 'value="19:00"')
        self.assertContains(page, 'value="60"')
        self.assertContains(page, "4h free that day")

    def test_saving_makes_an_accepted_sitting_and_starts_the_item(self):
        response = self.client.post(self.url, {"backlog_item": self.item.pk, "start_time": "19:00", "minutes": 45})
        self.assertRedirects(response, "/calendar/?date=2026-10-06")
        suggestion = Suggestion.objects.get()
        self.assertEqual(suggestion.status, Suggestion.Status.ACCEPTED)
        self.assertEqual((suggestion.date, suggestion.start_time, suggestion.end_time, suggestion.minutes), (TEST_DAY, time(19), time(19, 45), 45))
        self.assertIsNotNone(suggestion.responded_at)
        self.item.refresh_from_db()
        self.assertEqual(self.item.status, BacklogItem.Status.IN_PROGRESS)
        # It now blocks that time like any accepted sitting.
        self.assertEqual([(g.start, g.end) for g in free_intervals(self.user, TEST_DAY)], [(time(19, 45), time(23))])

    def test_overlapping_something_busy_is_refused(self):
        response = self.client.post(self.url, {"backlog_item": self.item.pk, "start_time": "18:30", "minutes": 60})
        self.assertContains(response, "overlaps Work")
        self.assertEqual(Suggestion.objects.count(), 0)

    def test_running_past_midnight_is_refused(self):
        response = self.client.post(self.url, {"backlog_item": self.item.pk, "start_time": "23:30", "minutes": 60})
        self.assertContains(response, "past midnight")
        self.assertEqual(Suggestion.objects.count(), 0)

    def test_someone_elses_item_is_not_a_choice(self):
        other = BacklogItem.objects.create(
            user=make_user("other"), title="Not yours", media_type=MediaType.BOOK, estimated_minutes=100
        )
        response = self.client.post(self.url, {"backlog_item": other.pk, "start_time": "19:00", "minutes": 30})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Suggestion.objects.count(), 0)

    def test_empty_backlog_says_so(self):
        self.item.delete()
        self.assertContains(self.client.get(self.url), "Your backlog is empty")

    def test_a_bad_date_falls_back_to_today(self):
        page = self.client.get("/calendar/plan/?date=nope")
        self.assertContains(page, f"Plan something for {timezone.localdate():%A, %B} {timezone.localdate().day}")


class CommitmentFromCalendarTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.client.force_login(self.user)
        RecurringBlock.objects.create(
            user=self.user, label="Work", weekday=TEST_DAY.weekday(), start_time=time(8), end_time=time(19)
        )

    def test_date_prefills_the_first_free_hour_of_that_day(self):
        page = self.client.get("/schedule/commitment/new/?date=2026-10-06&next=/calendar/%3Fdate%3D2026-10-06")
        self.assertContains(page, 'value="2026-10-06T19:00"')
        self.assertContains(page, 'value="2026-10-06T20:00"')
        self.assertContains(page, 'name="next" value="/calendar/?date=2026-10-06"')
        self.assertContains(page, 'href="/calendar/?date=2026-10-06"')

    def test_a_full_day_prefills_mid_morning(self):
        RecurringBlock.objects.create(
            user=self.user, label="More work", weekday=TEST_DAY.weekday(), start_time=time(19), end_time=time(23)
        )
        page = self.client.get("/schedule/commitment/new/?date=2026-10-06")
        self.assertContains(page, 'value="2026-10-06T09:00"')

    def test_saving_goes_back_where_it_came_from(self):
        response = self.client.post(
            "/schedule/commitment/new/",
            {"title": "Dentist", "starts_at": "2026-10-06T14:00", "ends_at": "2026-10-06T15:00", "next": "/calendar/?date=2026-10-06"},
        )
        self.assertRedirects(response, "/calendar/?date=2026-10-06")
        self.assertEqual(Commitment.objects.get().title, "Dentist")

    def test_next_must_be_on_this_site(self):
        response = self.client.post(
            "/schedule/commitment/new/",
            {"title": "Dentist", "starts_at": "2026-10-06T14:00", "ends_at": "2026-10-06T15:00", "next": "https://example.com/"},
        )
        self.assertRedirects(response, "/schedule/")

    def test_without_a_date_the_form_is_blank(self):
        page = self.client.get("/schedule/commitment/new/")
        self.assertNotContains(page, 'value="2026-10-06T')


class GapSplittingTests(TestCase):
    """A long free gap is shared out: a sitting, a break, then the next best thing."""

    def setUp(self):
        self.user = make_user()

    def add(self, title, media_type=MediaType.BOOK, minutes=600, priority=3):
        return BacklogItem.objects.create(
            user=self.user, title=title, media_type=media_type, estimated_minutes=minutes, priority=priority
        )

    def leave_gap(self, start, end):
        RecurringBlock.objects.create(
            user=self.user, label="Busy", weekday=TEST_DAY.weekday(), start_time=time(8), end_time=start
        )
        self.user.profile.day_end = end
        self.user.profile.save()

    def plan(self, **kwargs):
        made = suggest_for_day(self.user, TEST_DAY, persist=False, **kwargs)
        return [(s.backlog_item.title, s.start_time, s.end_time) for s in sorted(made, key=lambda s: s.start_time)]

    def test_a_free_day_gets_several_sittings_with_breaks_between(self):
        for name in ("A", "B", "C"):
            self.add(name)
        self.assertEqual(
            self.plan(),
            [("A", time(8), time(9, 15)), ("B", time(9, 30), time(10, 45)), ("C", time(11), time(12, 15))],
        )

    def test_the_limit_still_caps_the_day(self):
        for name in "ABCDE":
            self.add(name)
        self.assertEqual(len(self.plan(limit=2)), 2)
        self.assertEqual(len(self.plan()), 3)

    def test_a_remainder_too_short_to_sit_down_for_is_not_offered(self):
        self.leave_gap(time(19), time(21))  # 120 min: 75 + 15 break leaves 30
        self.add("A")
        self.add("B")
        self.assertEqual(self.plan(), [("A", time(19), time(20, 15))])

    def test_a_remainder_just_long_enough_is(self):
        self.leave_gap(time(19), time(21, 15))  # 135 min: 75 + 15 leaves exactly 45
        self.add("A")
        self.add("B")
        # 45 minutes holds one whole 25-minute chapter, so that is what B gets.
        self.assertEqual(self.plan(), [("A", time(19), time(20, 15)), ("B", time(20, 30), time(20, 55))])

    def test_a_film_that_fills_the_gap_leaves_nothing_to_split(self):
        self.leave_gap(time(19), time(21, 35))
        self.add("Dune", MediaType.MOVIE, 155, priority=5)
        self.add("A")
        self.assertEqual(self.plan(), [("Dune", time(19), time(21, 35))])

    def test_the_same_title_is_never_offered_twice_in_a_day(self):
        self.add("Only one")
        self.assertEqual(len(self.plan()), 1)

    def test_remainder_gap_maths(self):
        gap = Interval(time(8), time(12))
        self.assertEqual(_remainder_gap(gap, 75), Interval(time(9, 30), time(12)))
        self.assertIsNone(_remainder_gap(gap, 200))  # 240 - 215 = 25 left
        # A gap that runs to midnight keeps its last minute.
        self.assertEqual(_remainder_gap(Interval(time(22), time(23, 59)), 45), Interval(time(23), time(23, 59)))

    def test_the_week_planner_shares_days_out_too(self):
        self.add("A", priority=4)
        self.add("B")
        plan = suggest_for_week(self.user, start=WEEK_START, persist=False)
        titles_on_monday = [s.backlog_item.title for s in plan[WEEK_START]]
        self.assertEqual(titles_on_monday, ["A", "B"])


class DisplayNameTests(TestCase):
    """Shared reviews are signed with a display name, or not at all."""

    def setUp(self):
        self.reviewer = make_user("other", display_name="Kit")
        self.catalog = CatalogItem.objects.create(title="Dune", media_type=MediaType.BOOK, typical_minutes=600)
        self.entry = BacklogItem.objects.create(
            user=self.reviewer, catalog_item=self.catalog, title="Dune", media_type=MediaType.BOOK,
            estimated_minutes=600, status=BacklogItem.Status.COMPLETED,
        )
        self.review = Review.objects.create(user=self.reviewer, backlog_item=self.entry, rating=4, recommend=True)
        self.viewer = make_user()
        self.client.force_login(self.viewer)

    def test_display_name_replaces_the_username(self):
        self.assertEqual(self.review.reviewer_name, "Kit")
        page = self.client.get(self.catalog.get_absolute_url())
        self.assertContains(page, "Kit &middot;")
        self.assertNotContains(page, "other &middot;")

    def test_blank_display_name_falls_back_to_the_username(self):
        self.reviewer.profile.display_name = "  "
        self.reviewer.profile.save()
        self.assertEqual(Review.objects.get().reviewer_name, "other")

    def test_anonymous_review_is_signed_a_student(self):
        self.review.anonymous = True
        self.review.save()
        self.assertEqual(self.review.reviewer_name, "A student")
        page = self.client.get(self.catalog.get_absolute_url())
        self.assertContains(page, "A student &middot;")
        self.assertNotContains(page, "Kit")

    def test_setting_the_name_strips_and_saves(self):
        response = self.client.post("/reviews/name/", {"display_name": "  Sam K.  "})
        self.assertRedirects(response, "/reviews/")
        self.viewer.profile.refresh_from_db()
        self.assertEqual(self.viewer.profile.display_name, "Sam K.")
        self.assertContains(self.client.get("/reviews/"), "Sam K.")

    def test_clearing_the_name_goes_back_to_the_username(self):
        self.viewer.profile.display_name = "Sam K."
        self.viewer.profile.save()
        self.client.post("/reviews/name/", {"display_name": ""})
        self.viewer.profile.refresh_from_db()
        self.assertEqual(self.viewer.profile.public_name, "sam")

    def test_too_long_a_name_is_refused(self):
        self.client.post("/reviews/name/", {"display_name": "x" * 41})
        self.viewer.profile.refresh_from_db()
        self.assertEqual(self.viewer.profile.display_name, "")

    def test_review_form_offers_anonymity_and_names_the_signature(self):
        item = BacklogItem.objects.create(
            user=self.viewer, title="Done thing", media_type=MediaType.BOOK, estimated_minutes=100,
            status=BacklogItem.Status.COMPLETED,
        )
        page = self.client.get(f"/reviews/{item.pk}/write/")
        self.assertContains(page, 'name="anonymous"')
        self.assertContains(page, "signed <strong>sam</strong>")
        self.client.post(
            f"/reviews/{item.pk}/write/",
            {"rating": 5, "recommend": "yes", "headline": "", "body": "", "shared": "on", "anonymous": "on"},
        )
        self.assertTrue(Review.objects.get(backlog_item=item).anonymous)
        self.assertContains(self.client.get("/reviews/"), "shared without your name")


class TimeLogTests(TestCase):
    """Minutes logged by hand carry a date, so Progress can chart them."""

    def setUp(self):
        self.user = make_user()
        self.client.force_login(self.user)
        self.item = BacklogItem.objects.create(
            user=self.user, title="A book", media_type=MediaType.BOOK, estimated_minutes=600
        )
        self.url = f"/backlog/{self.item.pk}/log/"

    def test_form_defaults_to_today(self):
        page = self.client.get(self.url)
        self.assertContains(page, "Log time on A book")
        self.assertContains(page, f'value="{timezone.localdate():%Y-%m-%d}"')
        self.assertContains(page, "600 min left")

    def test_logging_adds_to_the_item_and_starts_it(self):
        response = self.client.post(self.url, {"date": "2026-10-05", "minutes": 40, "note": "On the bus"})
        self.assertRedirects(response, "/backlog/")
        log = TimeLog.objects.get()
        self.assertEqual((log.date, log.minutes, log.note, log.backlog_item), (date(2026, 10, 5), 40, "On the bus", self.item))
        self.item.refresh_from_db()
        self.assertEqual(self.item.minutes_completed, 40)
        self.assertEqual(self.item.status, BacklogItem.Status.IN_PROGRESS)

    def test_logging_the_last_minutes_finishes_it(self):
        self.item.minutes_completed = 580
        self.item.save()
        self.client.post(self.url, {"date": "2026-10-05", "minutes": 60})
        self.item.refresh_from_db()
        self.assertEqual(self.item.minutes_completed, 600)
        self.assertEqual(self.item.status, BacklogItem.Status.COMPLETED)
        self.assertIsNotNone(self.item.completed_at)

    def test_future_dates_are_refused(self):
        tomorrow = timezone.localdate() + timedelta(days=1)
        response = self.client.post(self.url, {"date": f"{tomorrow:%Y-%m-%d}", "minutes": 30})
        self.assertContains(response, "spent yet")
        self.assertEqual(TimeLog.objects.count(), 0)

    def test_zero_minutes_is_refused(self):
        response = self.client.post(self.url, {"date": "2026-10-05", "minutes": 0})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(TimeLog.objects.count(), 0)

    def test_next_is_honoured(self):
        page = self.client.get(self.url + "?next=/backlog/%3Fstatus%3Din_progress")
        self.assertContains(page, 'name="next" value="/backlog/?status=in_progress"')
        response = self.client.post(self.url, {"date": "2026-10-05", "minutes": 30, "next": "/backlog/?status=in_progress"})
        self.assertRedirects(response, "/backlog/?status=in_progress")

    def test_only_your_own_items(self):
        other = BacklogItem.objects.create(
            user=make_user("other"), title="Theirs", media_type=MediaType.BOOK, estimated_minutes=100
        )
        self.assertEqual(self.client.get(f"/backlog/{other.pk}/log/").status_code, 404)

    def test_backlog_page_links_to_it_for_unfinished_items_only(self):
        done = BacklogItem.objects.create(
            user=self.user, title="Done", media_type=MediaType.BOOK, estimated_minutes=100,
            minutes_completed=100, status=BacklogItem.Status.COMPLETED,
        )
        page = self.client.get("/backlog/")
        self.assertContains(page, f"/backlog/{self.item.pk}/log/")
        self.assertNotContains(page, f"/backlog/{done.pk}/log/")

    def test_progress_counts_logged_time_on_its_date(self):
        TimeLog.objects.create(user=self.user, backlog_item=self.item, date=date(2026, 10, 5), minutes=40)
        game = BacklogItem.objects.create(user=self.user, title="Hades", media_type=MediaType.GAME, estimated_minutes=1200)
        TimeLog.objects.create(user=self.user, backlog_item=game, date=date(2026, 9, 30), minutes=60)
        stats = progress_stats(self.user, today=TEST_DAY)
        self.assertEqual(stats["total_minutes"], 100)
        self.assertEqual(stats["month_minutes"], 40)  # September's game session is last month
        self.assertEqual(stats["month_sessions"], 1)
        by_type = {row["key"]: row["minutes"] for row in stats["by_type"]}
        self.assertEqual(by_type["book"], 40)
        self.assertEqual(by_type["game"], 0)
        daily = {entry["date"]: entry["minutes"] for entry in stats["daily"]}
        self.assertEqual(daily[date(2026, 10, 5)], 40)
        self.assertEqual(daily[date(2026, 9, 30)], 60)
        self.assertEqual(stats["current_streak"], 1)

    def test_logged_time_and_done_sittings_add_up(self):
        TimeLog.objects.create(user=self.user, backlog_item=self.item, date=TEST_DAY, minutes=40)
        Suggestion.objects.create(
            user=self.user, backlog_item=self.item, date=TEST_DAY, start_time=time(19), end_time=time(20),
            minutes=60, status=Suggestion.Status.DONE,
            responded_at=timezone.make_aware(datetime(2026, 10, 6, 20)),
        )
        stats = progress_stats(self.user, today=TEST_DAY)
        self.assertEqual(stats["total_minutes"], 100)
        self.assertEqual(stats["month_sessions"], 2)


class StaffWithoutProfileTests(TestCase):
    def test_a_fresh_profile_has_usable_waking_hours(self):
        # A profile made on the spot is used before any reload, so the
        # defaults have to be real times, not "08:00" strings.
        admin = get_user_model().objects.create_superuser("boss", "boss@example.com", "pw")
        profile = Profile.objects.create(user=admin)
        self.assertEqual((profile.day_start, profile.day_end), (time(8), time(23)))
        self.client.force_login(admin)
        self.assertEqual(self.client.get("/calendar/").status_code, 200)


# ------------------------------------------------------------ iteration 4, part two

from collections import Counter as _Counter  # noqa: E402

from django.contrib import admin as django_admin  # noqa: E402

from mediaflow.env import database_from_url  # noqa: E402

from .admin import CatalogItemAdmin  # noqa: E402
from .management.commands.import_catalog import Command as ImportCommand  # noqa: E402
from .management.commands.vapid_keys import generate_vapid_keys  # noqa: E402
from .models import PushSubscription, SentReminder  # noqa: E402
from .services import _webpush_send, push_due_reminders, push_payload  # noqa: E402


class CatalogLockTests(TestCase):
    """Hand edits to an imported row survive the next import."""

    def record(self, **overrides):
        from .catalog_import import Record

        values = {
            "source": "tmdb", "source_id": "movie-1", "title": "Arrival", "media_type": MediaType.MOVIE,
            "genre": "Science fiction", "creator": "Denis Villeneuve", "description": "Aliens.",
            "release_date": date(2016, 11, 11), "rating": 7.9, "typical_minutes": 116,
        }
        return Record(**{**values, **overrides})

    def test_a_locked_row_is_kept_as_it_was(self):
        save_record(self.record())
        row = CatalogItem.objects.get()
        row.typical_minutes = 120
        row.locked = True
        row.save()
        self.assertEqual(save_record(self.record(typical_minutes=116, title="Arrival (2016)")), "kept")
        row.refresh_from_db()
        self.assertEqual((row.typical_minutes, row.title), (120, "Arrival"))

    def test_an_unlocked_row_is_still_refreshed(self):
        save_record(self.record())
        self.assertEqual(save_record(self.record(typical_minutes=120)), "updated")
        self.assertEqual(CatalogItem.objects.get().typical_minutes, 120)

    def test_the_import_summary_names_kept_rows(self):
        summary = ImportCommand._summary(_Counter(created=1, kept=2), _Counter())
        self.assertIn("1 added", summary)
        self.assertIn("2 kept as edited by hand", summary)

    def fake_form(self, changed):
        return mock.Mock(changed_data=changed)

    def test_editing_an_imported_row_in_the_admin_locks_it(self):
        save_record(self.record())
        row = CatalogItem.objects.get()
        row.typical_minutes = 120
        CatalogItemAdmin(CatalogItem, django_admin.site).save_model(None, row, self.fake_form(["typical_minutes"]), True)
        self.assertTrue(CatalogItem.objects.get().locked)

    def test_unticking_keep_my_edits_is_respected(self):
        save_record(self.record())
        row = CatalogItem.objects.get()
        row.locked = False
        CatalogItemAdmin(CatalogItem, django_admin.site).save_model(None, row, self.fake_form(["locked"]), True)
        self.assertFalse(CatalogItem.objects.get().locked)

    def test_hand_entered_rows_are_not_locked_by_editing(self):
        row = CatalogItem.objects.create(title="Mine", media_type=MediaType.BOOK, typical_minutes=100)
        CatalogItemAdmin(CatalogItem, django_admin.site).save_model(None, row, self.fake_form(["title"]), True)
        self.assertFalse(CatalogItem.objects.get().locked)


class SeedCatalogTests(TestCase):
    def test_a_title_already_in_the_catalog_twice_does_not_stop_the_seed(self):
        # The curated row and an imported copy of the same film.
        curated = CatalogItem.objects.create(title="Dune", media_type=MediaType.MOVIE, typical_minutes=150)
        CatalogItem.objects.create(
            title="Dune", media_type=MediaType.MOVIE, typical_minutes=155, source="tmdb", source_id="438631"
        )
        out = io.StringIO()
        call_command("seed_catalog", stdout=out)
        curated.refresh_from_db()
        self.assertEqual(curated.typical_minutes, 155)  # refreshed from the seed list
        self.assertEqual(CatalogItem.objects.filter(title="Dune", media_type=MediaType.MOVIE).count(), 2)
        self.assertIn("Catalog ready", out.getvalue())

    def test_a_locked_curated_row_keeps_its_edits(self):
        row = CatalogItem.objects.create(title="Dune", media_type=MediaType.MOVIE, typical_minutes=150, locked=True)
        call_command("seed_catalog", stdout=io.StringIO())
        row.refresh_from_db()
        self.assertEqual(row.typical_minutes, 150)

    def test_running_twice_adds_nothing_the_second_time(self):
        call_command("seed_catalog", stdout=io.StringIO())
        count = CatalogItem.objects.count()
        out = io.StringIO()
        call_command("seed_catalog", stdout=out)
        self.assertEqual(CatalogItem.objects.count(), count)
        self.assertIn("(0 new)", out.getvalue())


class DatabaseUrlTests(TestCase):
    def test_postgres_url(self):
        entry = database_from_url("postgres://mediaflow:s3cret@db:5432/mediaflow?sslmode=require")
        self.assertEqual(entry["ENGINE"], "django.db.backends.postgresql")
        self.assertEqual((entry["NAME"], entry["USER"], entry["PASSWORD"]), ("mediaflow", "mediaflow", "s3cret"))
        self.assertEqual((entry["HOST"], entry["PORT"]), ("db", "5432"))
        self.assertEqual(entry["OPTIONS"], {"sslmode": "require"})
        self.assertEqual(entry["CONN_MAX_AGE"], 60)
        self.assertTrue(entry["CONN_HEALTH_CHECKS"])

    def test_postgresql_scheme_and_escaped_password(self):
        entry = database_from_url("postgresql://u:p%40ss@localhost/mf")
        self.assertEqual(entry["PASSWORD"], "p@ss")
        self.assertEqual(entry["PORT"], "")
        self.assertNotIn("OPTIONS", entry)

    def test_sqlite_relative_and_absolute_paths(self):
        relative = database_from_url("sqlite:///data/other.sqlite3", base_dir="/app")
        self.assertEqual(relative["ENGINE"], "django.db.backends.sqlite3")
        self.assertEqual(str(relative["NAME"]).replace("\\", "/"), "/app/data/other.sqlite3")
        absolute = database_from_url("sqlite:////var/lib/mf.sqlite3", base_dir="/app")
        self.assertEqual(str(absolute["NAME"]).replace("\\", "/"), "/var/lib/mf.sqlite3")

    def test_bad_urls_are_errors(self):
        for url in ("mysql://u@h/db", "postgres:///nohost", "sqlite://", "not a url"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                database_from_url(url)

    def test_settings_switch_to_postgres_on_database_url(self):
        result, s = settings_under({**PRODUCTION, "DATABASE_URL": "postgres://u:p@db:5432/mediaflow"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(s["database"], "mediaflow")

    def test_settings_refuse_a_database_url_they_do_not_understand(self):
        result, _ = settings_under({**PRODUCTION, "DATABASE_URL": "mysql://u:p@db/mediaflow"})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("DATABASE_URL must start with", result.stderr)


PUSH_ON = {"PUSH_ENABLED": True, "VAPID_PUBLIC_KEY": "BPublicKeyForTests", "VAPID_PRIVATE_KEY": "private-for-tests"}

SUBSCRIPTION = {
    "endpoint": "https://push.example.com/send/abc123",
    "expirationTime": None,
    "keys": {"p256dh": "BClientPublicKey", "auth": "authsecret"},
}


class PushEndpointTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.client.force_login(self.user)

    def subscribe(self, body=SUBSCRIPTION, **kwargs):
        return self.client.post(
            "/notifications/push/subscribe/", json.dumps(body), content_type="application/json", **kwargs
        )

    def test_the_service_worker_is_served_at_the_root_scope_to_anyone(self):
        self.client.logout()
        response = self.client.get("/sw.js")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/javascript")
        self.assertEqual(response["Service-Worker-Allowed"], "/")
        self.assertIn(b"showNotification", response.content)
        self.assertIn(b"notificationclick", response.content)

    def test_push_is_off_without_keys(self):
        self.assertEqual(self.client.get("/notifications/push/key/").json(), {"enabled": False, "public_key": ""})
        self.assertEqual(self.subscribe().status_code, 409)
        self.assertEqual(PushSubscription.objects.count(), 0)
        self.assertContains(self.client.get("/dashboard/"), 'data-push-key=""')

    @override_settings(**PUSH_ON)
    def test_the_page_hands_the_browser_the_public_key(self):
        page = self.client.get("/dashboard/")
        self.assertContains(page, 'data-push-key="BPublicKeyForTests"')
        self.assertContains(page, 'data-sw-url="/sw.js"')
        self.assertContains(page, 'data-subscribe-url="/notifications/push/subscribe/"')
        self.assertEqual(
            self.client.get("/notifications/push/key/").json(), {"enabled": True, "public_key": "BPublicKeyForTests"}
        )

    @override_settings(**PUSH_ON)
    def test_subscribing_saves_the_browser(self):
        response = self.subscribe(HTTP_USER_AGENT="TestBrowser/1.0")
        self.assertEqual(response.json(), {"ok": True, "count": 1})
        subscription = PushSubscription.objects.get()
        self.assertEqual(subscription.user, self.user)
        self.assertEqual(subscription.endpoint, SUBSCRIPTION["endpoint"])
        self.assertEqual(subscription.subscription_info(), {"endpoint": SUBSCRIPTION["endpoint"], "keys": SUBSCRIPTION["keys"]})
        self.assertEqual(subscription.user_agent, "TestBrowser/1.0")
        # Posting the same subscription again refreshes it rather than adding a copy.
        self.subscribe()
        self.assertEqual(PushSubscription.objects.count(), 1)

    @override_settings(**PUSH_ON)
    def test_a_browser_follows_whoever_is_signed_in(self):
        self.subscribe()
        other = make_user("other")
        self.client.force_login(other)
        self.subscribe()
        self.assertEqual(PushSubscription.objects.get().user, other)

    @override_settings(**PUSH_ON)
    def test_rubbish_is_refused(self):
        self.assertEqual(self.subscribe({"endpoint": "http://insecure.example.com/x", "keys": SUBSCRIPTION["keys"]}).status_code, 400)
        self.assertEqual(self.subscribe({"endpoint": SUBSCRIPTION["endpoint"], "keys": {"p256dh": ""}}).status_code, 400)
        self.assertEqual(self.subscribe([1, 2, 3]).status_code, 400)
        response = self.client.post("/notifications/push/subscribe/", "{not json", content_type="application/json")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.client.get("/notifications/push/subscribe/").status_code, 405)
        self.assertEqual(PushSubscription.objects.count(), 0)

    @override_settings(**PUSH_ON)
    def test_unsubscribing_removes_only_that_browser(self):
        self.subscribe()
        self.subscribe({**SUBSCRIPTION, "endpoint": "https://push.example.com/send/other"})
        response = self.client.post(
            "/notifications/push/unsubscribe/", json.dumps({"endpoint": SUBSCRIPTION["endpoint"]}),
            content_type="application/json",
        )
        self.assertEqual(response.json(), {"ok": True, "removed": 1})
        self.assertEqual(PushSubscription.objects.get().endpoint, "https://push.example.com/send/other")

    def test_unverified_users_are_not_bounced_off_the_push_routes(self):
        self.client.force_login(make_user("fresh", email_verified=False))
        self.assertEqual(self.client.get("/notifications/push/key/").status_code, 200)
        self.assertEqual(self.client.get("/sw.js").status_code, 200)


@override_settings(**PUSH_ON)
class PushSenderTests(TestCase):
    """push_due_reminders with a fake sender, on a fixed clock."""

    def setUp(self):
        self.user = make_user(notify_enabled=True)
        self.now = timezone.make_aware(datetime(2026, 10, 6, 18, 50))
        self.item = BacklogItem.objects.create(
            user=self.user, title="A book", media_type=MediaType.BOOK, estimated_minutes=600
        )
        self.subscription = PushSubscription.objects.create(
            user=self.user, endpoint="https://push.example.com/send/1", p256dh="k", auth="a"
        )
        self.sent = []

    def due_session(self, start=time(19), user=None):
        return Suggestion.objects.create(
            user=user or self.user, backlog_item=self.item, date=TEST_DAY, start_time=start,
            end_time=time(start.hour + 1, start.minute), minutes=60, status=Suggestion.Status.ACCEPTED,
        )

    def sender(self, outcome="sent"):
        def send(subscription, payload):
            self.sent.append((subscription.endpoint, json.loads(payload)))
            return outcome

        return send

    def test_a_due_reminder_is_pushed_once(self):
        self.due_session()
        counts = push_due_reminders(now=self.now, send=self.sender())
        self.assertEqual((counts["users"], counts["sent"]), (1, 1))
        endpoint, payload = self.sent[0]
        self.assertEqual(endpoint, self.subscription.endpoint)
        self.assertEqual(payload["title"], "A book")
        self.assertEqual(payload["body"], "1h planned · starts in 10 min")
        self.assertEqual(payload["url"], "/dashboard/")
        self.assertTrue(payload["tag"].startswith("session-"))
        self.assertEqual(SentReminder.objects.filter(user=self.user).count(), 1)
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.last_sent_at, self.now)

        # A minute later, nothing new.
        again = push_due_reminders(now=self.now + timedelta(minutes=1), send=self.sender())
        self.assertEqual(again["sent"], 0)
        self.assertEqual(len(self.sent), 1)

    def test_every_browser_of_the_user_gets_it(self):
        PushSubscription.objects.create(user=self.user, endpoint="https://push.example.com/send/2", p256dh="k", auth="a")
        self.due_session()
        counts = push_due_reminders(now=self.now, send=self.sender())
        self.assertEqual(counts["sent"], 2)
        self.assertEqual({endpoint for endpoint, _ in self.sent}, {"https://push.example.com/send/1", "https://push.example.com/send/2"})

    def test_nothing_due_means_nothing_sent(self):
        self.due_session(start=time(21))  # 2h10m away, lead is 15 min
        self.assertEqual(push_due_reminders(now=self.now, send=self.sender())["sent"], 0)
        self.assertEqual(SentReminder.objects.count(), 0)

    def test_users_who_turned_reminders_off_are_left_alone(self):
        self.user.profile.notify_enabled = False
        self.user.profile.save()
        self.due_session()
        self.assertEqual(push_due_reminders(now=self.now, send=self.sender())["sent"], 0)

    def test_a_gone_subscription_is_dropped(self):
        self.due_session()
        counts = push_due_reminders(now=self.now, send=self.sender("gone"))
        self.assertEqual(counts["gone"], 1)
        self.assertEqual(PushSubscription.objects.count(), 0)
        # Still recorded, so it is not retried every minute.
        self.assertEqual(SentReminder.objects.count(), 1)

    def test_a_failed_push_counts_the_failure_and_moves_on(self):
        self.due_session()
        counts = push_due_reminders(now=self.now, send=self.sender("error"))
        self.assertEqual(counts["error"], 1)
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.failures, 1)
        self.assertEqual(SentReminder.objects.count(), 1)

    def test_commitments_are_pushed_too(self):
        Commitment.objects.create(
            user=self.user, title="Dentist", starts_at=self.now + timedelta(minutes=5), ends_at=self.now + timedelta(hours=1)
        )
        push_due_reminders(now=self.now, send=self.sender())
        self.assertEqual(self.sent[0][1]["body"], "Commitment · starts in 5 min")

    def test_old_sent_rows_are_pruned(self):
        stale = SentReminder.objects.create(user=self.user, key="session-old")
        SentReminder.objects.filter(pk=stale.pk).update(sent_at=self.now - timedelta(days=3))
        push_due_reminders(now=self.now, send=self.sender())
        self.assertFalse(SentReminder.objects.filter(key="session-old").exists())

    @override_settings(PUSH_ENABLED=False)
    def test_off_means_off(self):
        self.due_session()
        self.assertEqual(push_due_reminders(now=self.now, send=self.sender()), _Counter())
        self.assertEqual(self.sent, [])

    def test_payload_for_something_starting_now(self):
        reminder = {"title": "Dune", "detail": "2h planned", "minutes_until": 0, "key": "session-1-x"}
        self.assertEqual(push_payload(reminder)["body"], "2h planned · starting now")

    def test_the_real_sender_maps_outcomes(self):
        from pywebpush import WebPushException

        with mock.patch("pywebpush.webpush") as webpush:
            self.assertEqual(_webpush_send(self.subscription, "{}"), "sent")
            self.assertEqual(webpush.call_args.kwargs["subscription_info"], self.subscription.subscription_info())
            self.assertEqual(webpush.call_args.kwargs["vapid_private_key"], "private-for-tests")
            self.assertTrue(webpush.call_args.kwargs["vapid_claims"]["sub"].startswith("mailto:"))
            webpush.side_effect = WebPushException("gone", response=mock.Mock(status_code=410))
            self.assertEqual(_webpush_send(self.subscription, "{}"), "gone")
            webpush.side_effect = WebPushException("nope", response=mock.Mock(status_code=500))
            self.assertEqual(_webpush_send(self.subscription, "{}"), "error")
            webpush.side_effect = OSError("network")
            self.assertEqual(_webpush_send(self.subscription, "{}"), "error")


class PushCommandTests(TestCase):
    def test_vapid_keys_prints_a_usable_pair(self):
        from py_vapid import Vapid

        out = io.StringIO()
        call_command("vapid_keys", stdout=out)
        text = out.getvalue()
        self.assertIn('VAPID_PUBLIC_KEY="', text)
        self.assertIn('VAPID_PRIVATE_KEY="', text)
        public, private = generate_vapid_keys()
        self.assertEqual(len(public), 87)  # an uncompressed P-256 point, base64url, no padding
        self.assertEqual(len(private), 43)
        Vapid.from_string(private)  # pywebpush can load it

    def test_two_runs_give_two_pairs(self):
        self.assertNotEqual(generate_vapid_keys(), generate_vapid_keys())

    def test_sending_refuses_to_run_without_keys(self):
        with self.assertRaisesMessage(CommandError, "VAPID_PUBLIC_KEY"):
            call_command("send_push_reminders")

    @override_settings(**PUSH_ON)
    def test_sending_once_reports_what_it_did(self):
        user = make_user(notify_enabled=True)
        item = BacklogItem.objects.create(user=user, title="A book", media_type=MediaType.BOOK, estimated_minutes=600)
        PushSubscription.objects.create(user=user, endpoint="https://push.example.com/send/1", p256dh="k", auth="a")
        now = timezone.localtime()
        starts = now + timedelta(minutes=5)
        Suggestion.objects.create(
            user=user, backlog_item=item, date=starts.date(), start_time=starts.time().replace(second=0, microsecond=0),
            end_time=(starts + timedelta(hours=1)).time(), minutes=60, status=Suggestion.Status.ACCEPTED,
        )
        out = io.StringIO()
        with mock.patch("planner.services._webpush_send", return_value="sent"):
            call_command("send_push_reminders", stdout=out)
        self.assertIn("1 user(s) due: 1 sent", out.getvalue())

    @override_settings(**PUSH_ON)
    def test_a_quiet_run_prints_nothing(self):
        out = io.StringIO()
        call_command("send_push_reminders", stdout=out)
        self.assertEqual(out.getvalue(), "")

    @override_settings(**PUSH_ON)
    def test_the_interval_must_be_positive(self):
        with self.assertRaisesMessage(CommandError, "--interval"):
            call_command("send_push_reminders", "--loop", "--interval", "0")


class DeploymentFilesTests(TestCase):
    """The new compose layer and the push service, checked as text like the others."""

    def read(self, name):
        return (PROJECT_ROOT / name).read_text(encoding="utf-8")

    def test_the_push_service_is_opt_in_and_shares_the_database_volume(self):
        compose = self.read("docker-compose.yml")
        self.assertIn('profiles: ["push"]', compose)
        self.assertIn("send_push_reminders", compose)
        self.assertIn("container_name: mediaflow-push", compose)
        self.assertEqual(compose.count("mediaflow-data:/data"), 2)

    def test_the_postgres_layer_sets_database_url_for_both_services(self):
        overlay = self.read("docker-compose.postgres.yml")
        self.assertEqual(overlay.count("DATABASE_URL: postgres://mediaflow:${POSTGRES_PASSWORD:?"), 2)
        self.assertIn("image: postgres:16-alpine", overlay)
        self.assertIn("mediaflow-postgres:/var/lib/postgresql/data", overlay)
        self.assertIn("pg_isready", overlay)
        self.assertIn("condition: service_healthy", overlay)

    def test_the_drivers_are_in_requirements(self):
        requirements = self.read("requirements.txt")
        self.assertIn("psycopg[binary]", requirements)
        self.assertIn("pywebpush", requirements)
        self.assertIn("playwright", self.read("requirements-dev.txt"))

    def test_no_real_vapid_key_is_committed(self):
        # A VAPID public key is 87 base64url characters starting with B.
        for name in (".env.example", "README.md", "docs/runbook.md", "docker-compose.yml"):
            self.assertNotRegex(self.read(name), r"\bB[A-Za-z0-9_-]{86}\b", name)

    def test_the_backup_script_uses_the_online_backup_and_keeps_a_limit(self):
        script = self.read("docker/backup.sh")
        self.assertIn("source.backup(copy)", script)
        self.assertIn("KEEP", script)
        self.assertNotIn("\r\n", script)


class TodayPanelPushNoteTests(TestCase):
    def test_my_week_has_the_note_the_script_rewrites(self):
        self.client.force_login(make_user())
        self.assertContains(self.client.get("/schedule/"), "data-push-note")


# ------------------------------------------------------------ iteration 4, part three: planner

from .models import BlockException  # noqa: E402
from .services import _end_time_for, busy_blocks, community_ratings, stale_sittings  # noqa: E402


class MoveAndCancelTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.client.force_login(self.user)
        self.item = BacklogItem.objects.create(user=self.user, title="A book", media_type=MediaType.BOOK, estimated_minutes=600)
        self.sitting = Suggestion.objects.create(
            user=self.user, backlog_item=self.item, date=TEST_DAY, start_time=time(19), end_time=time(20),
            minutes=60, status=Suggestion.Status.ACCEPTED,
        )

    def test_cancel_takes_an_accepted_sitting_off_the_calendar(self):
        response = self.client.post(f"/suggestions/{self.sitting.pk}/cancel/", {"next": "/calendar/?date=2026-10-06"})
        self.assertRedirects(response, "/calendar/?date=2026-10-06")
        self.assertEqual(Suggestion.objects.count(), 0)
        self.assertEqual([(g.start, g.end) for g in free_intervals(self.user, TEST_DAY)], [(time(8), time(23))])

    def test_only_accepted_sittings_can_be_cancelled(self):
        self.sitting.status = Suggestion.Status.PENDING
        self.sitting.save()
        self.assertEqual(self.client.post(f"/suggestions/{self.sitting.pk}/cancel/").status_code, 404)

    def test_the_move_form_is_prefilled_from_the_sitting(self):
        page = self.client.get(f"/calendar/plan/?move={self.sitting.pk}")
        self.assertContains(page, "Move A book")
        self.assertContains(page, 'value="19:00"')
        self.assertContains(page, 'value="60"')
        self.assertContains(page, 'value="2026-10-06"')
        self.assertContains(page, "Save the move")

    def test_moving_changes_day_time_and_length_in_place(self):
        response = self.client.post(
            f"/calendar/plan/?move={self.sitting.pk}",
            {"backlog_item": self.item.pk, "date": "2026-10-07", "start_time": "20:30", "minutes": 45},
        )
        self.assertRedirects(response, "/calendar/?date=2026-10-07")
        self.sitting.refresh_from_db()
        self.assertEqual((self.sitting.date, self.sitting.start_time, self.sitting.end_time, self.sitting.minutes), (date(2026, 10, 7), time(20, 30), time(21, 15), 45))
        self.assertEqual(Suggestion.objects.count(), 1)

    def test_a_sitting_can_move_within_its_own_time(self):
        # Its current slot does not count as busy against itself.
        response = self.client.post(
            f"/calendar/plan/?move={self.sitting.pk}",
            {"backlog_item": self.item.pk, "date": "2026-10-06", "start_time": "19:15", "minutes": 45},
        )
        self.assertEqual(response.status_code, 302)
        self.sitting.refresh_from_db()
        self.assertEqual(self.sitting.start_time, time(19, 15))

    def test_moving_onto_something_else_is_refused(self):
        Commitment.objects.create(
            user=self.user, title="Dinner",
            starts_at=timezone.make_aware(datetime(2026, 10, 6, 21)), ends_at=timezone.make_aware(datetime(2026, 10, 6, 22)),
        )
        response = self.client.post(
            f"/calendar/plan/?move={self.sitting.pk}",
            {"backlog_item": self.item.pk, "date": "2026-10-06", "start_time": "20:45", "minutes": 30},
        )
        self.assertContains(response, "overlaps Dinner")

    def test_you_cannot_move_someone_elses_sitting(self):
        other = make_user("other")
        theirs = Suggestion.objects.create(
            user=other, backlog_item=BacklogItem.objects.create(user=other, title="X", media_type=MediaType.BOOK, estimated_minutes=100),
            date=TEST_DAY, start_time=time(19), end_time=time(20), minutes=60, status=Suggestion.Status.ACCEPTED,
        )
        self.assertEqual(self.client.get(f"/calendar/plan/?move={theirs.pk}").status_code, 404)

    def test_the_card_offers_move_and_remove_for_planned_sittings(self):
        self.sitting.date = timezone.localdate()
        self.sitting.save()
        page = self.client.get("/dashboard/")
        self.assertContains(page, f"/calendar/plan/?move={self.sitting.pk}")
        self.assertContains(page, f"/suggestions/{self.sitting.pk}/cancel/")


class FollowUpTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.client.force_login(self.user)
        self.item = BacklogItem.objects.create(user=self.user, title="A book", media_type=MediaType.BOOK, estimated_minutes=600)
        self.now = timezone.make_aware(datetime(2026, 10, 6, 18, 0))

    def sitting(self, day, start, end, status=Suggestion.Status.ACCEPTED):
        return Suggestion.objects.create(
            user=self.user, backlog_item=self.item, date=day, start_time=start, end_time=end, minutes=60, status=status
        )

    def test_yesterdays_unmarked_sitting_is_stale(self):
        old = self.sitting(TEST_DAY - timedelta(days=1), time(19), time(20))
        self.assertEqual(stale_sittings(self.user, now=self.now), [old])

    def test_todays_sitting_is_stale_only_once_it_has_ended(self):
        ended = self.sitting(TEST_DAY, time(9), time(10))
        self.sitting(TEST_DAY, time(19), time(20))  # later today
        self.assertEqual(stale_sittings(self.user, now=self.now), [ended])

    def test_one_running_past_midnight_waits_until_tomorrow(self):
        night = self.sitting(TEST_DAY, time(23), time(1))
        self.assertEqual(stale_sittings(self.user, now=self.now), [])
        self.assertEqual(stale_sittings(self.user, now=self.now + timedelta(days=1)), [night])

    def test_done_and_pending_ones_are_not_asked_about(self):
        self.sitting(TEST_DAY - timedelta(days=1), time(19), time(20), status=Suggestion.Status.DONE)
        self.sitting(TEST_DAY - timedelta(days=1), time(19), time(20), status=Suggestion.Status.PENDING)
        self.assertEqual(stale_sittings(self.user, now=self.now), [])

    def test_today_asks_and_both_answers_work(self):
        old = self.sitting(timezone.localdate() - timedelta(days=2), time(19), time(20))
        page = self.client.get("/dashboard/")
        self.assertContains(page, 'data-panel="followup"')
        self.assertContains(page, "Did these happen?")
        self.client.post(f"/suggestions/{old.pk}/done/", {"next": "/dashboard/"})
        old.refresh_from_db()
        self.assertEqual(old.status, Suggestion.Status.DONE)
        self.item.refresh_from_db()
        self.assertEqual(self.item.minutes_completed, 60)
        self.assertNotContains(self.client.get("/dashboard/"), 'data-panel="followup"')

    def test_the_panel_can_be_hidden_like_the_others(self):
        self.sitting(timezone.localdate() - timedelta(days=2), time(19), time(20))
        self.client.post("/dashboard/panels/", {"hide": "followup"})
        self.assertNotContains(self.client.get("/dashboard/"), 'data-panel="followup"')


class BlockExceptionTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.client.force_login(self.user)
        self.block = RecurringBlock.objects.create(
            user=self.user, label="Lecture", weekday=TEST_DAY.weekday(), start_time=time(11), end_time=time(13)
        )

    def test_skipping_frees_that_date_only(self):
        response = self.client.post(f"/schedule/block/{self.block.pk}/skip/", {"date": "2026-10-06"})
        self.assertRedirects(response, "/calendar/?date=2026-10-06")
        self.assertEqual(BlockException.objects.count(), 1)
        self.assertEqual([(g.start, g.end) for g in free_intervals(self.user, TEST_DAY)], [(time(8), time(23))])
        next_week = TEST_DAY + timedelta(days=7)
        self.assertEqual([g.start for g in free_intervals(self.user, next_week)], [time(8), time(13)])

    def test_skipping_twice_is_harmless(self):
        self.client.post(f"/schedule/block/{self.block.pk}/skip/", {"date": "2026-10-06"})
        self.client.post(f"/schedule/block/{self.block.pk}/skip/", {"date": "2026-10-06"})
        self.assertEqual(BlockException.objects.count(), 1)

    def test_the_wrong_weekday_is_refused(self):
        self.client.post(f"/schedule/block/{self.block.pk}/skip/", {"date": "2026-10-07"})
        self.assertEqual(BlockException.objects.count(), 0)

    def test_restoring(self):
        exception = BlockException.objects.create(user=self.user, block=self.block, date=TEST_DAY)
        response = self.client.post(f"/schedule/skip/{exception.pk}/delete/")
        self.assertRedirects(response, "/schedule/")
        self.assertEqual(BlockException.objects.count(), 0)

    def test_a_day_off_skips_everything_that_day(self):
        RecurringBlock.objects.create(
            user=self.user, label="Shift", weekday=TEST_DAY.weekday(), start_time=time(17), end_time=time(21)
        )
        RecurringBlock.objects.create(user=self.user, label="Other day", weekday=(TEST_DAY.weekday() + 1) % 7, start_time=time(9), end_time=time(10))
        response = self.client.post("/schedule/day-off/", {"date": "2026-10-06"})
        self.assertRedirects(response, "/calendar/?date=2026-10-06")
        self.assertEqual(BlockException.objects.count(), 2)
        self.assertEqual([(g.start, g.end) for g in free_intervals(self.user, TEST_DAY)], [(time(8), time(23))])

    def test_the_calendar_shows_skip_and_restore(self):
        page = self.client.get("/calendar/?date=2026-10-06")
        self.assertContains(page, f"/schedule/block/{self.block.pk}/skip/")
        self.assertContains(page, "Day off")
        exception = BlockException.objects.create(user=self.user, block=self.block, date=TEST_DAY)
        page = self.client.get("/calendar/?date=2026-10-06")
        self.assertContains(page, f"/schedule/skip/{exception.pk}/delete/")
        self.assertContains(page, "line-through")

    def test_my_week_lists_coming_exceptions(self):
        future = timezone.localdate() + timedelta(days=(self.block.weekday - timezone.localdate().weekday()) % 7)
        exception = BlockException.objects.create(user=self.user, block=self.block, date=future)
        page = self.client.get("/schedule/")
        self.assertContains(page, "Skipped this time")
        self.assertContains(page, f"/schedule/skip/{exception.pk}/delete/")

    def test_the_planner_uses_the_freed_time(self):
        BacklogItem.objects.create(user=self.user, title="Short film", media_type=MediaType.MOVIE, estimated_minutes=110, priority=5)
        RecurringBlock.objects.create(user=self.user, label="Rest", weekday=TEST_DAY.weekday(), start_time=time(8), end_time=time(11))
        RecurringBlock.objects.create(user=self.user, label="Rest", weekday=TEST_DAY.weekday(), start_time=time(13), end_time=time(23))
        self.assertEqual(suggest_for_day(self.user, TEST_DAY, persist=False), [])
        BlockException.objects.create(user=self.user, block=self.block, date=TEST_DAY)
        (made,) = suggest_for_day(self.user, TEST_DAY, persist=False)
        self.assertEqual((made.backlog_item.title, made.start_time, made.minutes), ("Short film", time(11), 110))

    def test_someone_elses_block_cannot_be_skipped(self):
        other = RecurringBlock.objects.create(user=make_user("other"), label="X", weekday=TEST_DAY.weekday(), start_time=time(9), end_time=time(10))
        self.assertEqual(self.client.post(f"/schedule/block/{other.pk}/skip/", {"date": "2026-10-06"}).status_code, 404)


class UnitTrackingTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.client.force_login(self.user)

    def item(self, **kwargs):
        values = {"user": self.user, "title": "Severance", "media_type": MediaType.TV, "estimated_minutes": 1080}
        return BacklogItem.objects.create(**{**values, **kwargs})

    def test_position_is_derived_from_minutes_and_unit_length(self):
        show = self.item(minutes_completed=180)  # four 45-minute episodes
        self.assertEqual((show.unit_name, show.units_total, show.units_done, show.next_unit), ("episode", 24, 4, 5))
        self.assertEqual(show.progress_label, "Episode 5 of 24 next")

    def test_a_custom_episode_length_changes_the_count(self):
        show = self.item(unit_minutes=60, minutes_completed=180)
        self.assertEqual((show.units_total, show.next_unit), (18, 4))
        self.assertEqual(show.rules["chunk_minutes"], 60)
        # Sittings are rounded to whole episodes of that length.
        self.assertEqual(show.session_minutes_for(135), 120)

    def test_films_have_no_units(self):
        film = BacklogItem.objects.create(user=self.user, title="Dune", media_type=MediaType.MOVIE, estimated_minutes=155)
        self.assertEqual((film.unit_name, film.units_total, film.progress_label), ("", None, ""))

    def test_a_finished_item_has_no_next_unit(self):
        show = self.item(minutes_completed=1080, status=BacklogItem.Status.COMPLETED)
        self.assertIsNone(show.next_unit)
        self.assertEqual(show.progress_label, "")

    def test_units_label_for_a_sitting(self):
        show = self.item(minutes_completed=180)
        self.assertEqual(show.units_label(135), "episodes 5–7")
        self.assertEqual(show.units_label(45), "episode 5")
        book = BacklogItem.objects.create(user=self.user, title="B", media_type=MediaType.BOOK, estimated_minutes=100, minutes_completed=75)
        self.assertEqual(book.units_label(50), "chapter 4")  # only one chapter left

    def test_suggestions_say_which_episodes(self):
        self.item(minutes_completed=180, priority=5)
        RecurringBlock.objects.create(user=self.user, label="Busy", weekday=TEST_DAY.weekday(), start_time=time(8), end_time=time(20))
        (made,) = suggest_for_day(self.user, TEST_DAY, persist=False)
        self.assertIn("episodes 5–7", made.reason)

    def test_the_edit_form_takes_episodes_finished(self):
        show = self.item()
        response = self.client.post(
            f"/backlog/{show.pk}/edit/",
            {
                "title": "Severance", "media_type": "tv", "priority": 3, "estimated_minutes": 1080,
                "minutes_completed": 0, "unit_minutes": 60, "units_done": 3, "status": "in_progress", "notes": "",
            },
        )
        self.assertRedirects(response, "/backlog/")
        show.refresh_from_db()
        self.assertEqual((show.unit_minutes, show.minutes_completed), (60, 180))
        self.assertEqual(show.progress_label, "Episode 4 of 18 next")

    def test_the_edit_form_shows_the_current_episode_count(self):
        show = self.item(minutes_completed=180)
        page = self.client.get(f"/backlog/{show.pk}/edit/")
        self.assertContains(page, 'name="units_done" value="4"')

    def test_the_card_shows_the_position(self):
        show = self.item(minutes_completed=180)
        Suggestion.objects.create(
            user=self.user, backlog_item=show, date=timezone.localdate(), start_time=time(0, 5), end_time=time(0, 50),
            minutes=45, status=Suggestion.Status.ACCEPTED,
        )
        self.assertContains(self.client.get("/dashboard/"), "Episode 5 of 24 next")


class WeeklyGoalTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.client.force_login(self.user)
        self.item = BacklogItem.objects.create(user=self.user, title="A book", media_type=MediaType.BOOK, estimated_minutes=600)

    def log(self, day, minutes):
        TimeLog.objects.create(user=self.user, backlog_item=self.item, date=day, minutes=minutes)

    def test_this_week_runs_monday_to_today(self):
        tuesday = TEST_DAY  # a Tuesday
        self.log(tuesday - timedelta(days=1), 60)   # Monday
        self.log(tuesday, 30)                       # today
        self.log(tuesday - timedelta(days=2), 500)  # last Sunday, not this week
        stats = progress_stats(self.user, today=tuesday)
        self.assertEqual(stats["week_minutes"], 90)
        self.assertEqual(stats["weekly_goal"], 0)
        self.assertEqual(stats["goal_percent"], 0)

    def test_against_a_goal(self):
        self.user.profile.weekly_goal_minutes = 300
        self.user.profile.save()
        self.log(TEST_DAY, 150)
        stats = progress_stats(self.user, today=TEST_DAY)
        self.assertEqual((stats["goal_percent"], stats["goal_met"], stats["weekly_goal_time"]), (50, False, "5h"))
        self.log(TEST_DAY, 200)
        stats = progress_stats(self.user, today=TEST_DAY)
        self.assertEqual((stats["goal_percent"], stats["goal_met"]), (100, True))

    def test_saving_a_goal_from_my_week(self):
        response = self.client.post("/schedule/", {"save_goal": "1", "weekly_goal_minutes": 300})
        self.assertRedirects(response, "/schedule/")
        self.user.profile.refresh_from_db()
        self.assertEqual(self.user.profile.weekly_goal_minutes, 300)
        self.client.post("/schedule/", {"save_goal": "1", "weekly_goal_minutes": 20000})
        self.user.profile.refresh_from_db()
        self.assertEqual(self.user.profile.weekly_goal_minutes, 300)

    def test_today_shows_the_bar_only_with_a_goal(self):
        self.assertNotContains(self.client.get("/dashboard/"), "data-goal")
        self.user.profile.weekly_goal_minutes = 300
        self.user.profile.save()
        page = self.client.get("/dashboard/")
        self.assertContains(page, "data-goal")
        self.assertContains(page, "of 5h")

    def test_progress_has_the_week_card(self):
        page = self.client.get("/progress/")
        self.assertContains(page, "data-week-goal")
        self.assertContains(page, "Set a weekly goal")


class CommunityScoringTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.catalog = CatalogItem.objects.create(title="Loved", media_type=MediaType.BOOK, typical_minutes=600)
        self.other_catalog = CatalogItem.objects.create(title="Plain", media_type=MediaType.BOOK, typical_minutes=600)

    def review(self, catalog, rating, who="other", shared=True):
        reviewer = get_user_model().objects.filter(username=who).first() or make_user(who)
        entry = BacklogItem.objects.create(
            user=reviewer, catalog_item=catalog, title=catalog.title, media_type=MediaType.BOOK,
            estimated_minutes=600, status=BacklogItem.Status.COMPLETED,
        )
        return Review.objects.create(user=reviewer, backlog_item=entry, rating=rating, shared=shared)

    def test_ratings_come_from_other_students_shared_reviews(self):
        self.review(self.catalog, 5, "a")
        self.review(self.catalog, 4, "b")
        self.review(self.other_catalog, 1, "c", shared=False)
        # Your own review does not count toward what "other students" think.
        own_entry = BacklogItem.objects.create(user=self.user, catalog_item=self.other_catalog, title="Plain", media_type=MediaType.BOOK, estimated_minutes=600)
        Review.objects.create(user=self.user, backlog_item=own_entry, rating=5)
        self.assertEqual(community_ratings(self.user), {self.catalog.pk: 4.5})

    def test_the_bonus_scales_with_the_average(self):
        item = BacklogItem.objects.create(user=self.user, catalog_item=self.catalog, title="Loved", media_type=MediaType.BOOK, estimated_minutes=600)
        weights = only(community_bonus=10)
        score = lambda avg: score_item(item, FOUR_HOURS, {}, (), weights, {self.catalog.pk: avg})[0]
        self.assertEqual(score(5), 10)
        self.assertEqual(score(3), 0)
        self.assertEqual(score(1), -10)
        self.assertEqual(score_item(item, FOUR_HOURS, {}, (), weights, {})[0], 0)

    def test_a_loved_title_beats_an_unrated_equal_but_not_a_higher_priority(self):
        loved = BacklogItem.objects.create(user=self.user, catalog_item=self.catalog, title="Loved", media_type=MediaType.BOOK, estimated_minutes=600, priority=3)
        plain = BacklogItem.objects.create(user=self.user, catalog_item=self.other_catalog, title="Plain", media_type=MediaType.BOOK, estimated_minutes=600, priority=3)
        self.review(self.catalog, 5, "a")
        RecurringBlock.objects.create(user=self.user, label="Busy", weekday=TEST_DAY.weekday(), start_time=time(8), end_time=time(21))
        self.assertEqual(suggest_for_day(self.user, TEST_DAY, persist=False)[0].backlog_item, loved)
        plain.priority = 4
        plain.save()
        self.assertEqual(suggest_for_day(self.user, TEST_DAY, persist=False)[0].backlog_item, plain)

    def test_the_default_weight_is_half_a_step(self):
        self.assertEqual(DEFAULT_WEIGHTS.community_bonus, DEFAULT_WEIGHTS.per_priority_step / 2)


class MidnightSittingTests(TestCase):
    """An overnight schedule: up by 10:00, in bed by 02:00. Tonight's gap can run on past midnight."""

    def setUp(self):
        self.user = make_user(day_start=time(10), day_end=time(2))
        RecurringBlock.objects.create(
            user=self.user, label="Everything", weekday=NIGHT_DAY.weekday(), start_time=time(10), end_time=time(22, 30)
        )

    def evening(self):
        return free_intervals(self.user, NIGHT_DAY)[-1]

    def test_the_evening_gap_knows_how_far_past_midnight_it_can_run(self):
        gap = self.evening()
        self.assertEqual((gap.start, gap.end, gap.minutes), (time(22, 30), time(23, 59), 90))
        self.assertEqual(gap.extension, 120)  # to the 02:00 bedtime
        self.assertEqual(gap.usable, 210)
        # The next date still owns its small hours.
        self.assertEqual([(g.start, g.end) for g in free_intervals(self.user, NIGHT_DAY + timedelta(days=1))][0], (time(0), time(2)))

    def test_something_busy_early_next_morning_shortens_the_run(self):
        tomorrow = NIGHT_DAY + timedelta(days=1)
        Commitment.objects.create(
            user=self.user, title="Flight",
            starts_at=timezone.make_aware(datetime.combine(tomorrow, time(1))), ends_at=timezone.make_aware(datetime.combine(tomorrow, time(3))),
        )
        self.assertEqual(self.evening().extension, 60)

    def test_a_normal_schedule_never_extends(self):
        early = make_user("early")
        self.assertEqual(free_intervals(early, NIGHT_DAY)[-1].extension, 0)

    def test_a_film_that_only_fits_across_midnight_is_offered(self):
        film = BacklogItem.objects.create(user=self.user, title="Late film", media_type=MediaType.MOVIE, estimated_minutes=150, priority=5)
        (made,) = suggest_for_day(self.user, NIGHT_DAY, persist=False)
        self.assertEqual(made.backlog_item, film)
        self.assertEqual((made.date, made.start_time, made.end_time, made.minutes), (NIGHT_DAY, time(22, 30), time(1), 150))
        self.assertTrue(made.crosses_midnight)
        self.assertIn("3h 30m free", made.reason)

    def test_an_accepted_late_sitting_blocks_the_small_hours_of_the_next_date(self):
        film = BacklogItem.objects.create(user=self.user, title="Late film", media_type=MediaType.MOVIE, estimated_minutes=150)
        Suggestion.objects.create(
            user=self.user, backlog_item=film, date=NIGHT_DAY, start_time=time(22, 30), end_time=time(1), minutes=150,
            status=Suggestion.Status.ACCEPTED,
        )
        tomorrow = NIGHT_DAY + timedelta(days=1)
        self.assertEqual([(g.start, g.end) for g in free_intervals(self.user, tomorrow)][0], (time(1), time(2)))
        # The evening is taken to 23:59; only the small hours of that date (from the night before) are left.
        self.assertEqual([(g.start, g.end) for g in free_intervals(self.user, NIGHT_DAY)], [(time(0), time(2))])
        blocks = [(b.start, b.end, b.source) for b in busy_blocks(self.user, tomorrow)]
        self.assertIn((time(0), time(1), "planned"), blocks)

    def test_nothing_is_split_after_a_sitting_that_crossed_midnight(self):
        # Block the small hours of this date too, so the evening is the only gap.
        RecurringBlock.objects.create(
            user=self.user, label="Asleep", weekday=NIGHT_DAY.weekday(), start_time=time(0), end_time=time(2)
        )
        BacklogItem.objects.create(user=self.user, title="Late film", media_type=MediaType.MOVIE, estimated_minutes=150, priority=5)
        BacklogItem.objects.create(user=self.user, title="A book", media_type=MediaType.BOOK, estimated_minutes=600)
        made = suggest_for_day(self.user, NIGHT_DAY, persist=False)
        self.assertEqual([s.backlog_item.title for s in made], ["Late film"])

    def test_the_evening_is_spent_before_the_small_hours_of_the_same_date(self):
        # 22:30 to 02:00 can hold more than 00:00 to 02:00, so it is planned first.
        BacklogItem.objects.create(user=self.user, title="Late film", media_type=MediaType.MOVIE, estimated_minutes=150, priority=5)
        BacklogItem.objects.create(user=self.user, title="A book", media_type=MediaType.BOOK, estimated_minutes=600)
        made = sorted(suggest_for_day(self.user, NIGHT_DAY, persist=False), key=lambda s: s.start_time)
        self.assertEqual([(s.backlog_item.title, s.start_time) for s in made], [("A book", time(0)), ("Late film", time(22, 30))])

    def test_a_sitting_that_ends_at_midnight_exactly_stays_on_its_date(self):
        self.assertEqual(_end_time_for(22 * 60 + 30, 90), time(23, 59))
        self.assertEqual(_end_time_for(22 * 60 + 30, 91), time(0, 1))
        self.assertEqual(_end_time_for(20 * 60, 60), time(21))

    def test_reminders_fire_on_the_start_time(self):
        film = BacklogItem.objects.create(user=self.user, title="Late film", media_type=MediaType.MOVIE, estimated_minutes=150)
        Suggestion.objects.create(
            user=self.user, backlog_item=film, date=NIGHT_DAY, start_time=time(22, 30), end_time=time(1), minutes=150,
            status=Suggestion.Status.ACCEPTED,
        )
        now = timezone.make_aware(datetime.combine(NIGHT_DAY, time(22, 20)))
        self.assertEqual([r["title"] for r in upcoming_reminders(self.user, now=now, lead=15)], ["Late film"])

    def test_the_day_panel_flags_it(self):
        film = BacklogItem.objects.create(user=self.user, title="Late film", media_type=MediaType.MOVIE, estimated_minutes=150)
        Suggestion.objects.create(
            user=self.user, backlog_item=film, date=NIGHT_DAY, start_time=time(22, 30), end_time=time(1), minutes=150,
            status=Suggestion.Status.PENDING,
        )
        self.client.force_login(self.user)
        page = self.client.get(f"/calendar/?date={NIGHT_DAY:%Y-%m-%d}")
        self.assertContains(page, "past midnight")
        self.assertContains(page, "10:30 PM – 1:00 AM")


# ------------------------------------------------------------ iteration 4, part four: account

from .management.commands.make_icons import icon_pixels, png_bytes  # noqa: E402


class SettingsPageTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.client.force_login(self.user)

    def test_the_page_gathers_every_setting(self):
        page = self.client.get("/settings/")
        for text in ("Waking hours", "Weekly goal", "Shared reviews", "Panels on Today", "Reminders", "Change password", "Download", "Delete"):
            self.assertContains(page, text)
        self.assertContains(page, 'name="panels"')
        self.assertContains(page, "/settings/export/")
        self.assertContains(page, "/settings/delete/")

    def test_the_nav_links_to_it(self):
        self.assertContains(self.client.get("/dashboard/"), 'href="/settings/"')

    def test_each_form_saves_on_its_own(self):
        self.client.post("/settings/", {"save_hours": "1", "day_start": "09:00", "day_end": "01:00"})
        self.client.post("/settings/", {"save_goal": "1", "weekly_goal_minutes": 240})
        self.client.post("/settings/", {"save_notifications": "1", "notify_enabled": "on", "notify_lead_minutes": 30})
        response = self.client.post("/settings/", {"save_name": "1", "display_name": "Sam K."})
        self.assertRedirects(response, "/settings/#name", fetch_redirect_response=False)
        self.user.profile.refresh_from_db()
        profile = self.user.profile
        self.assertEqual((profile.day_start, profile.day_end), (time(9), time(1)))
        self.assertEqual(profile.weekly_goal_minutes, 240)
        self.assertEqual((profile.notify_enabled, profile.notify_lead_minutes), (True, 30))
        self.assertEqual(profile.display_name, "Sam K.")

    def test_a_bad_value_stays_on_the_page_with_the_error(self):
        response = self.client.post("/settings/", {"save_hours": "1", "day_start": "09:00", "day_end": "09:00"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "can&#x27;t be the same time")

    def test_panels_saved_here_come_back_here(self):
        response = self.client.post("/dashboard/panels/", {"panels": ["day"], "next": "/settings/#panels"})
        self.assertRedirects(response, "/settings/#panels", fetch_redirect_response=False)
        self.user.profile.refresh_from_db()
        self.assertIn("suggestions", self.user.profile.hidden_panel_keys)

    @override_settings(PUSH_ENABLED=True, VAPID_PUBLIC_KEY="k", VAPID_PRIVATE_KEY="p")
    def test_subscribed_browsers_are_listed_and_removable(self):
        PushSubscription.objects.create(
            user=self.user, endpoint="https://push.example.com/send/1", p256dh="k", auth="a", user_agent="Firefox on a laptop"
        )
        page = self.client.get("/settings/")
        self.assertContains(page, "Firefox on a laptop")
        response = self.client.post(
            "/notifications/push/unsubscribe/", {"endpoint": "https://push.example.com/send/1", "next": "/settings/#reminders"}
        )
        self.assertRedirects(response, "/settings/#reminders", fetch_redirect_response=False)
        self.assertEqual(PushSubscription.objects.count(), 0)

    def test_without_push_the_list_says_so(self):
        self.assertContains(self.client.get("/settings/"), "not set up on this server")


class ExportAndDeleteTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.client.force_login(self.user)
        catalog = CatalogItem.objects.create(title="Dune", media_type=MediaType.MOVIE, typical_minutes=155)
        self.item = BacklogItem.objects.create(
            user=self.user, catalog_item=catalog, title="Dune", media_type=MediaType.MOVIE, estimated_minutes=155, notes="Saw the trailer"
        )
        Suggestion.objects.create(
            user=self.user, backlog_item=self.item, date=TEST_DAY, start_time=time(19), end_time=time(21, 35), minutes=155,
            status=Suggestion.Status.DONE,
        )
        TimeLog.objects.create(user=self.user, backlog_item=self.item, date=TEST_DAY, minutes=30, note="trailer")
        Review.objects.create(user=self.user, backlog_item=self.item, rating=5, recommend=True, headline="Big")
        block = RecurringBlock.objects.create(user=self.user, label="Lecture", weekday=1, start_time=time(11), end_time=time(12))
        BlockException.objects.create(user=self.user, block=block, date=TEST_DAY)
        Commitment.objects.create(
            user=self.user, title="Dentist",
            starts_at=timezone.make_aware(datetime(2026, 10, 6, 14)), ends_at=timezone.make_aware(datetime(2026, 10, 6, 15)),
        )

    def test_export_is_a_json_download_with_everything(self):
        response = self.client.get("/settings/export/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("attachment; filename=\"mediaflow-sam-", response["Content-Disposition"])
        data = json.loads(response.content)
        self.assertEqual(data["account"]["username"], "sam")
        self.assertEqual(data["settings"]["day_start"], "08:00:00")
        self.assertEqual(data["backlog"][0]["catalog_title"], "Dune")
        self.assertEqual(data["backlog"][0]["notes"], "Saw the trailer")
        self.assertEqual(data["sittings"][0]["status"], "done")
        self.assertEqual(data["time_logs"][0]["note"], "trailer")
        self.assertEqual(data["reviews"][0]["headline"], "Big")
        self.assertEqual(data["recurring_blocks"][0]["skipped_on"], ["2026-10-06"])
        self.assertEqual(data["commitments"][0]["title"], "Dentist")

    def test_only_your_own_data_is_exported(self):
        other = make_user("other")
        BacklogItem.objects.create(user=other, title="Theirs", media_type=MediaType.BOOK, estimated_minutes=100)
        data = json.loads(self.client.get("/settings/export/").content)
        self.assertEqual([item["title"] for item in data["backlog"]], ["Dune"])

    def test_deleting_needs_the_right_password_and_the_box(self):
        self.client.post("/settings/delete/", {"password": "wrong", "confirm": "on"})
        self.assertTrue(get_user_model().objects.filter(username="sam").exists())
        response = self.client.post("/settings/delete/", {"password": "test-password"})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(get_user_model().objects.filter(username="sam").exists())

    def test_deleting_removes_everything_and_signs_out(self):
        response = self.client.post("/settings/delete/", {"password": "test-password", "confirm": "on"})
        self.assertRedirects(response, "/", fetch_redirect_response=False)
        self.assertFalse(get_user_model().objects.filter(username="sam").exists())
        for model in (BacklogItem, Suggestion, TimeLog, Review, RecurringBlock, BlockException, Commitment, Profile):
            self.assertEqual(model.objects.count(), 0, model.__name__)
        self.assertEqual(self.client.get("/dashboard/").status_code, 302)  # signed out

    def test_the_confirm_page_warns_and_offers_the_export(self):
        page = self.client.get("/settings/delete/")
        self.assertContains(page, "cannot be undone")
        self.assertContains(page, "/settings/export/")


class InstallableAppTests(TestCase):
    def read(self, name):
        return (PROJECT_ROOT / name).read_text(encoding="utf-8")

    def test_the_page_links_the_manifest_and_icons(self):
        self.client.force_login(make_user())
        page = self.client.get("/dashboard/")
        self.assertContains(page, 'rel="manifest"')
        self.assertContains(page, "manifest.webmanifest")
        self.assertContains(page, 'rel="apple-touch-icon"')
        self.assertContains(page, 'name="theme-color"')

    def test_the_manifest_is_valid_and_points_at_real_files(self):
        manifest = json.loads(self.read("static/manifest.webmanifest"))
        self.assertEqual(manifest["start_url"], "/dashboard/")
        self.assertEqual(manifest["display"], "standalone")
        for icon in manifest["icons"]:
            self.assertTrue((PROJECT_ROOT / icon["src"].lstrip("/")).exists(), icon["src"])
        purposes = {icon["purpose"] for icon in manifest["icons"]}
        self.assertEqual(purposes, {"any", "maskable"})

    def test_the_icon_generator_writes_real_pngs(self):
        data = png_bytes(icon_pixels(32))
        self.assertTrue(data.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertIn(b"IHDR", data)
        self.assertTrue(data.endswith(b"IEND\xaeB`\x82"))
        pixels = icon_pixels(32)
        self.assertEqual(pixels[0][0][3], 0)       # transparent corner
        self.assertEqual(pixels[16][2][3], 255)    # painted edge
        self.assertEqual(len(pixels), 32)

    def test_the_committed_icons_match_the_generator(self):
        for size in (192, 512):
            self.assertEqual((PROJECT_ROOT / "static" / "icons" / f"icon-{size}.png").read_bytes(), png_bytes(icon_pixels(size)))


# ------------------------------------------------------------ iteration 4, part five: imports and lookup

from django.core.files.uploadedfile import SimpleUploadedFile  # noqa: E402

from .catalog_import import lookup_title, saved_item, source_keys  # noqa: E402
from .imports import apply_media, apply_schedule, guess_kind, read_ics, read_media_csv  # noqa: E402

SAMPLE_ICS = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Test//EN
BEGIN:VEVENT
UID:1
SUMMARY:CSE 3311 Lecture
DTSTART;TZID=America/Chicago:20260824T110000
DTEND;TZID=America/Chicago:20260824T122000
RRULE:FREQ=WEEKLY;BYDAY=MO,WE;UNTIL=20991211T055959Z
END:VEVENT
BEGIN:VEVENT
UID:2
SUMMARY:Library shift
DTSTART;TZID=America/Chicago:20260825T090000
DTEND;TZID=America/Chicago:20260825T150000
RRULE:FREQ=WEEKLY
END:VEVENT
BEGIN:VEVENT
UID:3
SUMMARY:Dentist\\, teeth
DTSTART:20261106T190000Z
DTEND:20261106T200000Z
END:VEVENT
BEGIN:VEVENT
UID:4
SUMMARY:Old meeting
DTSTART:20200101T190000Z
DTEND:20200101T200000Z
END:VEVENT
BEGIN:VEVENT
UID:5
SUMMARY:Spring break
DTSTART;VALUE=DATE:20990309
DTEND;VALUE=DATE:20990314
END:VEVENT
BEGIN:VEVENT
UID:6
SUMMARY:Ended last year
DTSTART;TZID=America/Chicago:20200113T100000
DTEND;TZID=America/Chicago:20200113T110000
RRULE:FREQ=WEEKLY;BYDAY=MO;UNTIL=20200501T000000Z
END:VEVENT
BEGIN:VEVENT
UID:7
SUMMARY:Monthly thing
DTSTART;TZID=America/Chicago:20260901T100000
DTEND;TZID=America/Chicago:20260901T110000
RRULE:FREQ=MONTHLY
END:VEVENT
BEGIN:VEVENT
UID:8
SUMMARY:Folded
  line event
DTSTART;TZID=America/Chicago:20261202T140000
DURATION:PT1H30M
END:VEVENT
END:VCALENDAR
"""


class IcsReadTests(TestCase):
    def setUp(self):
        self.now = timezone.make_aware(datetime(2026, 10, 6, 12))
        self.plan = read_ics(SAMPLE_ICS, now=self.now)

    def test_weekly_repeats_become_blocks_per_weekday(self):
        blocks = [(label, kind, weekday, start, end) for label, kind, weekday, start, end in self.plan.blocks]
        self.assertIn(("CSE 3311 Lecture", RecurringBlock.Kind.CLASS, 0, time(11), time(12, 20)), blocks)
        self.assertIn(("CSE 3311 Lecture", RecurringBlock.Kind.CLASS, 2, time(11), time(12, 20)), blocks)
        # No BYDAY: the weekday of the first occurrence (a Tuesday).
        self.assertIn(("Library shift", RecurringBlock.Kind.WORK, 1, time(9), time(15)), blocks)
        self.assertEqual(len(blocks), 3)

    def test_one_off_future_events_become_commitments_in_local_time(self):
        titles = {title: (start, end) for title, start, end in self.plan.commitments}
        self.assertIn("Dentist, teeth", titles)  # the escaped comma is unescaped
        start, end = titles["Dentist, teeth"]
        self.assertEqual(timezone.localtime(start).hour, 13)  # 19:00 UTC is 13:00 in Chicago once DST has ended
        self.assertEqual(end - start, timedelta(hours=1))
        self.assertIn("Folded line event", titles)
        start, end = titles["Folded line event"]
        self.assertEqual(end - start, timedelta(hours=1, minutes=30))

    def test_the_rest_is_skipped_with_a_reason(self):
        self.assertEqual(self.plan.skipped["in the past"], 1)
        self.assertEqual(self.plan.skipped["all-day"], 1)
        self.assertEqual(self.plan.skipped["already ended"], 1)
        self.assertEqual(self.plan.skipped["repeats monthly"], 1)

    def test_an_empty_or_odd_file_is_an_empty_plan(self):
        self.assertTrue(read_ics("").empty)
        self.assertTrue(read_ics("BEGIN:VCALENDAR\nEND:VCALENDAR\n").empty)
        plan = read_ics("BEGIN:VCALENDAR\nBEGIN:VEVENT\nSUMMARY:x\nDTSTART:garbage\nEND:VEVENT\nEND:VCALENDAR\n")
        self.assertEqual(plan.skipped["unreadable start time"], 1)

    def test_kind_guessing(self):
        self.assertEqual(guess_kind("MATH-2425 Recitation"), RecurringBlock.Kind.CLASS)
        self.assertEqual(guess_kind("Shift at the library"), RecurringBlock.Kind.WORK)
        self.assertEqual(guess_kind("Study group"), RecurringBlock.Kind.STUDY)
        self.assertEqual(guess_kind("Gym"), RecurringBlock.Kind.OTHER)

    def test_applying_creates_rows_once(self):
        user = make_user()
        counts = apply_schedule(user, self.plan)
        self.assertEqual((counts["blocks"], counts["commitments"]), (3, 2))
        self.assertEqual(user.recurring_blocks.count(), 3)
        self.assertEqual(user.commitments.count(), 2)
        again = apply_schedule(user, self.plan)
        self.assertEqual((again["blocks already there"], again["commitments already there"]), (3, 2))
        self.assertEqual(user.recurring_blocks.count(), 3)


class ScheduleImportViewTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.client.force_login(self.user)

    def upload(self, content, name="schedule.ics"):
        return SimpleUploadedFile(name, content.encode("utf-8"), content_type="text/calendar")

    def test_the_form_is_linked_from_my_week(self):
        self.assertContains(self.client.get("/schedule/"), "/schedule/import/")
        self.assertContains(self.client.get("/schedule/import/"), 'enctype="multipart/form-data"')

    def test_a_calendar_file_fills_the_week(self):
        with mock.patch("planner.imports.timezone.now", return_value=timezone.make_aware(datetime(2026, 10, 6, 12))):
            response = self.client.post("/schedule/import/", {"file": self.upload(SAMPLE_ICS)})
        self.assertRedirects(response, "/schedule/", fetch_redirect_response=False)
        self.assertEqual(self.user.recurring_blocks.count(), 3)
        self.assertEqual(self.user.commitments.count(), 2)
        page = self.client.get("/schedule/")
        self.assertContains(page, "Imported 3 blocks, 2 commitments.")
        self.assertContains(page, "Left out")

    def test_not_a_calendar(self):
        response = self.client.post("/schedule/import/", {"file": self.upload("hello", "notes.txt")})
        self.assertContains(response, "not a calendar")
        self.assertEqual(self.user.recurring_blocks.count(), 0)

    def test_nothing_usable_says_why(self):
        only_past = "BEGIN:VCALENDAR\nBEGIN:VEVENT\nSUMMARY:Old\nDTSTART:20200101T190000Z\nDTEND:20200101T200000Z\nEND:VEVENT\nEND:VCALENDAR\n"
        response = self.client.post("/schedule/import/", {"file": self.upload(only_past)})
        self.assertContains(response, "in the past 1")


LETTERBOXD_CSV = """Date,Name,Year,Letterboxd URI
2026-10-01,Dune,2021,https://boxd.it/abc
2026-10-02,Some Obscure Film,1999,https://boxd.it/def
2026-10-03,,2000,https://boxd.it/ghi
"""

LETTERBOXD_RATINGS = """Date,Name,Year,Letterboxd URI,Rating
2026-10-01,Arrival,2016,https://boxd.it/xyz,4.5
"""

GOODREADS_CSV = """Book Id,Title,Author,Author l-f,Additional Authors,ISBN,ISBN13,My Rating,Average Rating,Publisher,Binding,Number of Pages,Year Published,Original Publication Year,Date Read,Date Added,Bookshelves,Bookshelves with positions,Exclusive Shelf,My Review,Spoiler,Private Notes,Read Count,Owned Copies
1,Project Hail Mary,Andy Weir,"Weir, Andy",,,,0,4.5,Ballantine,Hardcover,476,2021,2021,,2026/10/01,to-read,to-read (#1),to-read,,,,0,0
2,The Fifth Season,N. K. Jemisin,"Jemisin, N. K.",,,,5,4.3,Orbit,Paperback,468,2015,2015,2026/09/01,2026/08/01,,,read,,,,1,0
3,Piranesi,Susanna Clarke,"Clarke, Susanna",,,,0,4.2,Bloomsbury,Hardcover,245,2020,2020,,2026/10/01,currently-reading,currently-reading (#1),currently-reading,,,,0,0
"""


class MediaCsvReadTests(TestCase):
    def test_letterboxd_watchlist(self):
        plan = read_media_csv(LETTERBOXD_CSV)
        self.assertEqual(plan.source, "letterboxd")
        self.assertEqual([(r.title, r.media_type, r.year, r.status) for r in plan.rows], [
            ("Dune", MediaType.MOVIE, 2021, BacklogItem.Status.BACKLOG),
            ("Some Obscure Film", MediaType.MOVIE, 1999, BacklogItem.Status.BACKLOG),
        ])
        self.assertEqual(plan.skipped["no title"], 1)

    def test_letterboxd_ratings_are_watched(self):
        (row,) = read_media_csv(LETTERBOXD_RATINGS).rows
        self.assertEqual((row.status, row.rating), (BacklogItem.Status.COMPLETED, 4))

    def test_goodreads_shelves_pages_and_ratings(self):
        plan = read_media_csv(GOODREADS_CSV)
        self.assertEqual(plan.source, "goodreads")
        rows = {row.title: row for row in plan.rows}
        self.assertEqual(rows["Project Hail Mary"].status, BacklogItem.Status.BACKLOG)
        self.assertEqual(rows["Project Hail Mary"].minutes, round(476 * 1.3))
        self.assertEqual(rows["Project Hail Mary"].creator, "Andy Weir")
        self.assertEqual((rows["The Fifth Season"].status, rows["The Fifth Season"].rating), (BacklogItem.Status.COMPLETED, 5))
        self.assertEqual(rows["Piranesi"].status, BacklogItem.Status.IN_PROGRESS)

    def test_anything_else_is_none(self):
        self.assertIsNone(read_media_csv("a,b,c\n1,2,3\n"))
        self.assertIsNone(read_media_csv(""))

    def test_a_byte_order_mark_is_fine(self):
        self.assertEqual(read_media_csv("﻿" + LETTERBOXD_CSV).source, "letterboxd")


class MediaImportApplyTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.dune = CatalogItem.objects.create(title="Dune", media_type=MediaType.MOVIE, typical_minutes=155, release_date=date(2021, 10, 22))
        CatalogItem.objects.create(title="Dune", media_type=MediaType.MOVIE, typical_minutes=137, release_date=date(1984, 12, 14), source="tmdb", source_id="movie-841")
        self.fifth = CatalogItem.objects.create(title="The Fifth Season", media_type=MediaType.BOOK, typical_minutes=600)

    def test_catalog_matches_prefer_the_same_year(self):
        counts = apply_media(self.user, read_media_csv(LETTERBOXD_CSV))
        self.assertEqual((counts["matched"], counts["added by title"]), (1, 1))
        dune = self.user.backlog_items.get(title="Dune")
        self.assertEqual((dune.catalog_item, dune.estimated_minutes), (self.dune, 155))
        obscure = self.user.backlog_items.get(title="Some Obscure Film")
        self.assertEqual((obscure.catalog_item, obscure.estimated_minutes), (None, 120))
        self.assertIn("Imported from Letterboxd", obscure.notes)

    def test_running_again_adds_nothing(self):
        apply_media(self.user, read_media_csv(LETTERBOXD_CSV))
        counts = apply_media(self.user, read_media_csv(LETTERBOXD_CSV))
        self.assertEqual(counts["already on your backlog"], 2)
        self.assertEqual(self.user.backlog_items.count(), 2)

    def test_goodreads_shelves_land_as_statuses_with_private_ratings(self):
        counts = apply_media(self.user, read_media_csv(GOODREADS_CSV))
        self.assertEqual(counts["ratings"], 1)
        fifth = self.user.backlog_items.get(title="The Fifth Season")
        self.assertEqual((fifth.status, fifth.minutes_completed, fifth.catalog_item), (BacklogItem.Status.COMPLETED, 600, self.fifth))
        self.assertIsNotNone(fifth.completed_at)
        self.assertEqual((fifth.review.rating, fifth.review.shared), (5, False))
        phm = self.user.backlog_items.get(title="Project Hail Mary")
        self.assertEqual((phm.status, phm.estimated_minutes), (BacklogItem.Status.BACKLOG, round(476 * 1.3)))
        self.assertEqual(self.user.backlog_items.get(title="Piranesi").status, BacklogItem.Status.IN_PROGRESS)

    def test_the_view_end_to_end(self):
        self.client.force_login(self.user)
        self.assertContains(self.client.get("/backlog/"), "/backlog/import/")
        upload = SimpleUploadedFile("watchlist.csv", LETTERBOXD_CSV.encode("utf-8"), content_type="text/csv")
        response = self.client.post("/backlog/import/", {"file": upload, "status": "backlog"})
        self.assertRedirects(response, "/backlog/", fetch_redirect_response=False)
        self.assertContains(self.client.get("/backlog/"), "Read 2 rows from Letterboxd")
        self.assertEqual(self.user.backlog_items.count(), 2)

    def test_the_view_refuses_other_files(self):
        self.client.force_login(self.user)
        upload = SimpleUploadedFile("x.csv", b"a,b\n1,2\n", content_type="text/csv")
        response = self.client.post("/backlog/import/", {"file": upload, "status": "backlog"})
        self.assertContains(response, "does not look like")

    def test_finished_default_marks_rows_completed(self):
        self.client.force_login(self.user)
        upload = SimpleUploadedFile("watched.csv", LETTERBOXD_CSV.encode("utf-8"), content_type="text/csv")
        self.client.post("/backlog/import/", {"file": upload, "status": "completed"})
        self.assertEqual(self.user.backlog_items.filter(status=BacklogItem.Status.COMPLETED).count(), 2)


def _lookup_routes(query_title="Arrival"):
    """A FakeFetcher that answers a title search on all three services."""
    book_doc = {
        "key": "/works/OL1W", "title": query_title, "author_name": ["Ted Chiang"], "first_publish_year": 2002,
        "number_of_pages_median": 281, "ratings_average": 4.3, "subject": ["Science fiction"],
    }
    movie = {
        "id": 329865, "title": query_title, "runtime": 116, "overview": "Aliens.", "release_date": "2016-11-11",
        "vote_average": 7.9, "genres": [{"name": "Science Fiction"}], "credits": {"crew": [{"job": "Director", "name": "Denis Villeneuve"}]},
    }
    game_row = {"id": 42, "name": query_title, "playtime": 10, "released": "2020-01-01", "genres": [{"name": "Puzzle"}], "rating": 4.0}
    return FakeFetcher({
        "/search.json": lambda params: {"docs": [book_doc]} if params.get("title") == query_title else {"docs": []},
        "/works/OL1W.json": {"description": "A linguist and some aliens."},
        "/3/search/movie": {"results": [{"id": 329865}]},
        "/3/movie/329865": movie,
        "/3/search/tv": {"results": []},
        "/api/games": {"results": [game_row]},
        "/api/games/42": {"playtime": 10, "developers": [{"name": "Studio"}], "description_raw": "Blocks."},
    })


class LookupTests(TestCase):
    def test_source_keys_reads_the_environment(self):
        self.assertEqual(source_keys({}), {"books": ""})
        self.assertEqual(set(source_keys({"TMDB_API_KEY": "k", "RAWG_API_KEY": "r"})), {"books", "movies", "tv", "games"})

    def test_lookup_asks_each_usable_source_and_saves_what_comes_back(self):
        fetcher = _lookup_routes()
        items, failures = lookup_title("Arrival", {"books": "", "movies": "k", "tv": "k", "games": "r"}, lambda cls: fetcher)
        self.assertEqual(failures, [])
        self.assertEqual(sorted((item.title, item.media_type) for item in items), [("Arrival", "book"), ("Arrival", "game"), ("Arrival", "movie")])
        self.assertEqual(CatalogItem.objects.count(), 3)
        self.assertIn("/3/search/movie", fetcher.paths())
        self.assertIn("/api/games", fetcher.paths())

    def test_lookup_can_be_narrowed_to_one_type(self):
        items, _ = lookup_title("Arrival", {"books": "", "movies": "k"}, lambda cls: _lookup_routes(), media_type=MediaType.BOOK)
        self.assertEqual([item.media_type for item in items], ["book"])

    def test_a_curated_duplicate_is_returned_not_copied(self):
        curated = CatalogItem.objects.create(title="Arrival", media_type=MediaType.MOVIE, typical_minutes=116, release_date=date(2016, 1, 1))
        items, _ = lookup_title("Arrival", {"movies": "k"}, lambda cls: _lookup_routes())
        self.assertEqual(items, [curated])
        self.assertEqual(CatalogItem.objects.count(), 1)

    def test_a_source_that_fails_is_reported_and_the_rest_still_run(self):
        from .catalog_import import ImportFailure

        class Failing(FakeFetcher):
            def get_json(self, url, params=None, headers=None):
                if "/3/" in url:
                    raise ImportFailure("TMDB is down")
                return super().get_json(url, params, headers)

        routes = _lookup_routes()
        fetcher = Failing(routes.routes)
        items, failures = lookup_title("Arrival", {"books": "", "movies": "k"}, lambda cls: fetcher)
        self.assertEqual(failures, ["movies"])
        self.assertEqual([item.media_type for item in items], ["book"])

    def test_saved_item_finds_the_row(self):
        from .catalog_import import Record

        record = Record(
            source="tmdb", source_id="movie-1", title="X", media_type=MediaType.MOVIE, typical_minutes=100,
            genre="", creator="", description="", release_date=None, rating=None,
        )
        self.assertIsNone(saved_item(record))
        save_record(record)
        self.assertEqual(saved_item(record).source_id, "movie-1")


class LookupViewTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.client.force_login(self.user)

    def test_discover_offers_the_lookup_only_with_a_search_term(self):
        self.assertNotContains(self.client.get("/discover/"), "data-lookup")
        with mock.patch.dict(os.environ, {"TMDB_API_KEY": "k", "RAWG_API_KEY": ""}):
            page = self.client.get("/discover/?q=Arrival")
        self.assertContains(page, "data-lookup")
        self.assertContains(page, "Look it up")
        self.assertContains(page, "books, movies, tv")

    def test_without_any_source_it_says_so(self):
        with mock.patch.dict(os.environ, {"TMDB_API_KEY": "", "RAWG_API_KEY": ""}):
            with mock.patch("planner.views.source_keys", return_value={}):
                page = self.client.get("/discover/?q=Arrival")
        self.assertContains(page, "No catalog sources are set up")

    def test_the_button_runs_the_lookup_and_comes_back_to_the_search(self):
        with mock.patch("planner.views.lookup_title", return_value=([CatalogItem(title="Arrival", media_type="movie", typical_minutes=116)], [])) as lookup:
            with mock.patch.dict(os.environ, {"TMDB_API_KEY": "k"}):
                response = self.client.post("/discover/lookup/", {"q": "Arrival", "media_type": "movie"})
        self.assertRedirects(response, "/discover/?q=Arrival&media_type=movie", fetch_redirect_response=False)
        self.assertEqual(lookup.call_args.args[0], "Arrival")
        self.assertEqual(lookup.call_args.kwargs["media_type"], "movie")

    def test_a_short_query_is_sent_back(self):
        response = self.client.post("/discover/lookup/", {"q": "a"})
        self.assertRedirects(response, "/discover/")

    def test_failures_become_a_message(self):
        with mock.patch("planner.views.lookup_title", return_value=([], ["movies"])):
            with mock.patch.dict(os.environ, {"TMDB_API_KEY": "k"}):
                self.client.post("/discover/lookup/", {"q": "Arrival"})
        self.assertContains(self.client.get("/discover/?q=Arrival"), "Could not reach movies")


# ------------------------------------------------------------ iteration 4, part six: polish and the last features

from .models import ApiToken, WatchInvite  # noqa: E402
from .services import DEFAULT_WEIGHTS, Interval, ScoringWeights, recent_rejection_counts  # noqa: E402,F811
from .services import (  # noqa: E402
    accept_invite,
    make_api_token,
    next_in_series,
    part_of_day,
    semester_overview,
    shared_slots,
    time_of_day_preferences,
    user_for_api_token,
)


class AccessibilityTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.client.force_login(self.user)

    def test_skip_link_main_landmark_and_live_messages(self):
        page = self.client.get("/dashboard/")
        self.assertContains(page, 'class="skip-link" href="#main"')
        self.assertContains(page, '<main id="main"')
        self.assertContains(page, 'aria-current="page"')
        self.assertContains(page, 'aria-label="Main"')

    def test_calendar_cells_are_described(self):
        Commitment.objects.create(
            user=self.user, title="Dentist",
            starts_at=timezone.make_aware(datetime(2026, 10, 6, 14)), ends_at=timezone.make_aware(datetime(2026, 10, 6, 16)),
        )
        page = self.client.get("/calendar/?year=2026&month=10&date=2026-10-06")
        self.assertContains(page, 'aria-label="Tuesday, October 6, 2h busy, 1 item, selected"')
        self.assertContains(page, 'aria-current="date"')

    def test_destructive_buttons_name_their_target(self):
        item = BacklogItem.objects.create(user=self.user, title="A book", media_type=MediaType.BOOK, estimated_minutes=600)
        self.assertContains(self.client.get("/backlog/"), 'aria-label="Remove A book from your backlog"')
        Suggestion.objects.create(
            user=self.user, backlog_item=item, date=timezone.localdate(), start_time=time(0, 5), end_time=time(0, 50),
            minutes=45, status=Suggestion.Status.ACCEPTED,
        )
        self.assertContains(self.client.get("/dashboard/"), 'aria-label="Take A book off the calendar"')

    def test_the_stylesheet_has_a_focus_ring_and_a_skip_link(self):
        css = (PROJECT_ROOT / "static" / "src" / "app.css").read_text(encoding="utf-8")
        self.assertIn(":focus-visible", css)
        self.assertIn(".skip-link", css)
        built = (PROJECT_ROOT / "static" / "css" / "app.css").read_text(encoding="utf-8")
        self.assertIn("skip-link", built)


class SnoozeTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.client.force_login(self.user)
        RecurringBlock.objects.create(user=self.user, label="Busy", weekday=TEST_DAY.weekday(), start_time=time(8), end_time=time(19))
        self.item = BacklogItem.objects.create(user=self.user, title="A book", media_type=MediaType.BOOK, estimated_minutes=600)

    def test_later_drops_the_suggestion_and_hides_the_title_until_tomorrow(self):
        (made,) = suggest_for_day(self.user, TEST_DAY)
        response = self.client.post(f"/suggestions/{made.pk}/later/", {"next": "/dashboard/"})
        self.assertRedirects(response, "/dashboard/", fetch_redirect_response=False)
        self.assertEqual(Suggestion.objects.count(), 0)
        self.item.refresh_from_db()
        self.assertEqual(self.item.snoozed_until, TEST_DAY + timedelta(days=1))
        self.assertEqual(suggest_for_day(self.user, TEST_DAY, persist=False), [])
        self.assertEqual(len(suggest_for_day(self.user, TEST_DAY + timedelta(days=1), persist=False)), 1)

    def test_later_carries_no_penalty(self):
        (made,) = suggest_for_day(self.user, TEST_DAY)
        self.client.post(f"/suggestions/{made.pk}/later/")
        self.assertEqual(recent_rejection_counts(self.user, TEST_DAY + timedelta(days=1)), {})

    def test_only_pending_suggestions_can_be_snoozed(self):
        sitting = Suggestion.objects.create(
            user=self.user, backlog_item=self.item, date=TEST_DAY, start_time=time(19), end_time=time(20), minutes=60,
            status=Suggestion.Status.ACCEPTED,
        )
        self.assertEqual(self.client.post(f"/suggestions/{sitting.pk}/later/").status_code, 404)

    def test_the_card_offers_later_next_to_not_today(self):
        made = Suggestion.objects.create(
            user=self.user, backlog_item=self.item, date=timezone.localdate(), start_time=time(23, 30), end_time=time(23, 59), minutes=25,
        )
        page = self.client.get("/dashboard/")
        self.assertContains(page, f"/suggestions/{made.pk}/later/")
        self.assertContains(page, f"/suggestions/{made.pk}/reject/")


class TimeOfDayTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.book = BacklogItem.objects.create(user=self.user, title="A book", media_type=MediaType.BOOK, estimated_minutes=600)
        self.show = BacklogItem.objects.create(user=self.user, title="A show", media_type=MediaType.TV, estimated_minutes=1000)

    def answer(self, item, start, status, days_ago=1):
        Suggestion.objects.create(
            user=self.user, backlog_item=item, date=TEST_DAY - timedelta(days=days_ago), start_time=start,
            end_time=time(start.hour + 1, 0), minutes=60, status=status,
        )

    def test_parts_of_day(self):
        self.assertEqual([part_of_day(time(h)) for h in (6, 11, 12, 16, 17, 20, 21, 23)],
                         ["morning", "morning", "afternoon", "afternoon", "evening", "evening", "night", "night"])

    def test_preferences_come_from_answers_with_a_minimum(self):
        self.answer(self.book, time(8), Suggestion.Status.ACCEPTED)
        self.assertEqual(time_of_day_preferences(self.user, TEST_DAY), {})  # one answer is not enough
        self.answer(self.book, time(9), Suggestion.Status.DONE)
        self.answer(self.show, time(21), Suggestion.Status.REJECTED)
        self.answer(self.show, time(22), Suggestion.Status.REJECTED)
        self.answer(self.show, time(19), Suggestion.Status.ACCEPTED)
        self.answer(self.show, time(19), Suggestion.Status.REJECTED)
        self.assertEqual(time_of_day_preferences(self.user, TEST_DAY), {("book", "morning"): 1.0, ("tv", "night"): -1.0, ("tv", "evening"): 0.0})

    def test_old_answers_are_forgotten(self):
        self.answer(self.book, time(8), Suggestion.Status.ACCEPTED, days_ago=90)
        self.answer(self.book, time(9), Suggestion.Status.ACCEPTED, days_ago=90)
        self.assertEqual(time_of_day_preferences(self.user, TEST_DAY), {})
        self.assertEqual(time_of_day_preferences(self.user, TEST_DAY, memory_days=120), {("book", "morning"): 1.0})

    def test_the_bonus_is_applied_per_gap(self):
        weights = only(time_of_day_bonus=10)
        preferences = {("book", "evening"): 1.0, ("book", "morning"): -0.5}
        self.assertEqual(score_item(self.book, FOUR_HOURS, {}, (), weights, None, preferences)[0], 10)  # 18:00 is evening
        morning = Interval(time(8), time(12))
        self.assertEqual(score_item(self.book, morning, {}, (), weights, None, preferences)[0], -5)
        self.assertEqual(score_item(self.show, FOUR_HOURS, {}, (), weights, None, preferences)[0], 0)

    def test_films_after_dinner_win_once_the_user_has_said_so(self):
        film = BacklogItem.objects.create(user=self.user, title="A film", media_type=MediaType.MOVIE, estimated_minutes=100)
        RecurringBlock.objects.create(user=self.user, label="Busy", weekday=TEST_DAY.weekday(), start_time=time(8), end_time=time(19))
        # Level priorities: the film has a built-in lead (whole-film bonus), so flip it with rejections first.
        for _ in range(2):
            self.answer(film, time(19), Suggestion.Status.REJECTED)
            self.answer(self.book, time(19), Suggestion.Status.ACCEPTED)
        picked = suggest_for_day(self.user, TEST_DAY, persist=False)[0].backlog_item
        self.assertEqual(picked, self.book)

    def test_weights_validate(self):
        with self.assertRaises(ValueError):
            ScoringWeights(time_of_day_min_answers=0)


class SemesterViewTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.client.force_login(self.user)
        self.monday = date(2026, 10, 5)
        RecurringBlock.objects.create(user=self.user, label="Lecture", weekday=0, start_time=time(9), end_time=time(11))
        RecurringBlock.objects.create(user=self.user, label="Shift", weekday=2, start_time=time(13), end_time=time(17))

    def test_weeks_sum_recurring_time_exceptions_and_commitments(self):
        BlockException.objects.create(user=self.user, block=self.user.recurring_blocks.get(label="Shift"), date=date(2026, 10, 14))
        Commitment.objects.create(
            user=self.user, title="Exam",
            starts_at=timezone.make_aware(datetime(2026, 10, 20, 9)), ends_at=timezone.make_aware(datetime(2026, 10, 20, 12)),
        )
        overview = semester_overview(self.user, start=self.monday, weeks=3)
        minutes = [week["minutes"] for week in overview["weeks"]]
        self.assertEqual(minutes, [360, 120, 540])  # a skipped shift, then an exam week
        self.assertEqual(overview["busiest"]["start"], date(2026, 10, 19))
        self.assertEqual(overview["lightest"]["start"], date(2026, 10, 12))
        self.assertEqual([c.title for c in overview["weeks"][2]["commitments"]], ["Exam"])
        self.assertEqual(overview["weeks"][0]["share"], round(360 / 540 * 100))
        self.assertEqual(overview["average_minutes"], 340)

    def test_the_page_renders_and_is_linked(self):
        page = self.client.get("/semester/")
        self.assertContains(page, "How busy the term looks")
        self.assertContains(page, "data-week")
        self.assertContains(page, "Busiest week")
        self.assertContains(self.client.get("/schedule/"), "/semester/")
        self.assertContains(self.client.get("/calendar/"), "/semester/")

    def test_weeks_can_be_chosen_within_limits(self):
        self.assertEqual(len(self.client.get("/semester/?weeks=8").context["overview"]["weeks"]), 8)
        self.assertEqual(len(self.client.get("/semester/?weeks=99").context["overview"]["weeks"]), 26)
        self.assertEqual(len(self.client.get("/semester/?weeks=x").context["overview"]["weeks"]), 16)

    def test_an_empty_schedule_says_so(self):
        self.user.recurring_blocks.all().delete()
        page = self.client.get("/semester/")
        self.assertContains(page, "Nothing recurring yet")


class SeriesTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.client.force_login(self.user)
        self.one = CatalogItem.objects.create(title="Dune", media_type=MediaType.MOVIE, typical_minutes=155, series="Dune", series_order=1, release_date=date(2021, 10, 22))
        self.two = CatalogItem.objects.create(title="Dune: Part Two", media_type=MediaType.MOVIE, typical_minutes=166, series="Dune", series_order=2, release_date=date(2024, 3, 1))
        self.three = CatalogItem.objects.create(title="Dune: Part Three", media_type=MediaType.MOVIE, typical_minutes=160, series="Dune", series_order=3)
        CatalogItem.objects.create(title="Dune (novel)", media_type=MediaType.BOOK, typical_minutes=1100, series="Dune")

    def test_next_in_series_is_ordered_and_skips_what_you_have(self):
        self.assertEqual(next_in_series(self.one), [self.two, self.three])
        self.assertEqual(next_in_series(self.two), [self.three])
        BacklogItem.objects.create(user=self.user, catalog_item=self.two, title="Dune: Part Two", media_type=MediaType.MOVIE, estimated_minutes=166)
        self.assertEqual(next_in_series(self.one, self.user), [self.three])
        self.assertEqual(next_in_series(CatalogItem.objects.create(title="Solo", media_type=MediaType.MOVIE, typical_minutes=100)), [])

    def test_release_dates_order_a_series_without_numbers(self):
        a = CatalogItem.objects.create(title="A", media_type=MediaType.BOOK, typical_minutes=100, series="Earthsea", release_date=date(1968, 1, 1))
        b = CatalogItem.objects.create(title="B", media_type=MediaType.BOOK, typical_minutes=100, series="Earthsea", release_date=date(1971, 1, 1))
        c = CatalogItem.objects.create(title="C", media_type=MediaType.BOOK, typical_minutes=100, series="Earthsea", release_date=date(1972, 1, 1))
        self.assertEqual(next_in_series(b), [c])
        self.assertEqual(next_in_series(a), [b, c])

    def test_the_catalog_page_shows_the_series_and_what_comes_next(self):
        page = self.client.get(self.one.get_absolute_url())
        self.assertContains(page, "3 in the catalog")
        self.assertContains(page, "data-series")
        self.assertContains(page, "Dune: Part Two")
        self.assertContains(page, "/watch-together/")

    def test_reviews_offers_what_comes_next_after_a_finished_one(self):
        BacklogItem.objects.create(
            user=self.user, catalog_item=self.one, title="Dune", media_type=MediaType.MOVIE, estimated_minutes=155,
            status=BacklogItem.Status.COMPLETED, completed_at=timezone.now(),
        )
        page = self.client.get("/reviews/")
        self.assertContains(page, "data-up-next")
        self.assertContains(page, "Dune: Part Two")
        self.assertContains(page, "after Dune")

    def test_tmdb_collections_fill_the_series(self):
        from .catalog_import import MovieSource

        movie = {
            "id": 1, "title": "Dune Messiah", "runtime": 155, "overview": "", "release_date": "2028-10-22", "vote_average": 8.0,
            "genres": [], "credits": {"crew": []}, "belongs_to_collection": {"id": 9, "name": "Dune Collection"},
        }
        record = MovieSource(FakeFetcher({}), "k")._record(1, movie)
        self.assertEqual(record.series, "Dune")
        save_record(record)
        self.assertEqual(CatalogItem.objects.get(source_id="movie-1").series, "Dune")


class WatchTogetherTests(TestCase):
    def setUp(self):
        self.host = make_user("sam")
        self.guest = make_user("kit")
        self.client.force_login(self.host)
        self.film = CatalogItem.objects.create(title="Arrival", media_type=MediaType.MOVIE, typical_minutes=116)
        # Both busy on the test day until 19:00; the host is also busy 21:00 on.
        for person in (self.host, self.guest):
            RecurringBlock.objects.create(user=person, label="Day", weekday=TEST_DAY.weekday(), start_time=time(8), end_time=time(19))
        RecurringBlock.objects.create(user=self.host, label="Late", weekday=TEST_DAY.weekday(), start_time=time(21), end_time=time(23))
        self.url = f"/media/{self.film.pk}/watch-together/"

    def test_shared_slots_intersect_both_free_times(self):
        slots = shared_slots(self.host, self.guest, 116, start=TEST_DAY, days=1)
        self.assertEqual(slots, [(TEST_DAY, time(19), time(20, 56))])
        self.assertEqual(shared_slots(self.host, self.guest, 150, start=TEST_DAY, days=1), [])
        # The next day both are free all day.
        slots = shared_slots(self.host, self.guest, 116, start=TEST_DAY, days=2)
        self.assertEqual(slots[1][0], TEST_DAY + timedelta(days=1))

    def test_the_form_finds_times_then_sends(self):
        with mock.patch("planner.views.shared_slots", return_value=[(TEST_DAY, time(19), time(20, 56))]):
            page = self.client.post(self.url, {"guest": "kit", "message": "Popcorn"})
            self.assertContains(page, "Times free for you and kit")
            self.assertContains(page, 'value="2026-10-06|19:00"')
            response = self.client.post(self.url, {"guest": "kit", "message": "Popcorn", "slot": "2026-10-06|19:00"})
        self.assertRedirects(response, self.film.get_absolute_url(), fetch_redirect_response=False)
        invite = WatchInvite.objects.get()
        self.assertEqual((invite.host, invite.guest, invite.title, invite.minutes), (self.host, self.guest, "Arrival", 116))
        self.assertEqual((invite.date, invite.start_time, invite.end_time, invite.status), (TEST_DAY, time(19), time(20, 56), "pending"))
        self.assertEqual(invite.message, "Popcorn")

    def test_bad_guests_are_refused(self):
        self.assertContains(self.client.post(self.url, {"guest": "nobody"}), "No MediaFlow account")
        self.assertContains(self.client.post(self.url, {"guest": "sam"}), "That is you")
        make_user("fresh", email_verified=False)
        self.assertContains(self.client.post(self.url, {"guest": "fresh"}), "not confirmed")
        self.assertEqual(WatchInvite.objects.count(), 0)

    def test_a_stale_slot_is_refused(self):
        with mock.patch("planner.views.shared_slots", return_value=[]):
            response = self.client.post(self.url, {"guest": "kit", "slot": "2026-10-06|19:00"})
        self.assertContains(response, "no longer free")
        self.assertEqual(WatchInvite.objects.count(), 0)

    def invite(self):
        return WatchInvite.objects.create(
            host=self.host, guest=self.guest, catalog_item=self.film, title="Arrival", media_type=MediaType.MOVIE,
            minutes=116, date=TEST_DAY, start_time=time(19), end_time=time(20, 56),
        )

    def test_accepting_puts_the_sitting_on_both_calendars(self):
        invite = self.invite()
        self.client.force_login(self.guest)
        response = self.client.post(f"/invites/{invite.pk}/accept/")
        self.assertRedirects(response, "/dashboard/", fetch_redirect_response=False)
        invite.refresh_from_db()
        self.assertEqual(invite.status, WatchInvite.Status.ACCEPTED)
        for person in (self.host, self.guest):
            sitting = person.suggestions.get()
            self.assertEqual((sitting.date, sitting.start_time, sitting.status), (TEST_DAY, time(19), Suggestion.Status.ACCEPTED))
            self.assertEqual(sitting.backlog_item.catalog_item, self.film)
            self.assertEqual(sitting.backlog_item.status, BacklogItem.Status.IN_PROGRESS)
        self.assertIn("with sam", self.guest.suggestions.get().reason)
        # Now neither of them is free then.
        self.assertEqual([(g.start, g.end) for g in free_intervals(self.guest, TEST_DAY)], [(time(20, 56), time(23))])

    def test_accepting_reuses_an_existing_backlog_entry(self):
        existing = BacklogItem.objects.create(user=self.guest, catalog_item=self.film, title="Arrival", media_type=MediaType.MOVIE, estimated_minutes=116)
        accept_invite(self.invite())
        self.assertEqual(self.guest.backlog_items.count(), 1)
        self.assertEqual(self.guest.suggestions.get().backlog_item, existing)

    def test_declining_and_withdrawing(self):
        invite = self.invite()
        self.client.force_login(self.guest)
        self.client.post(f"/invites/{invite.pk}/decline/")
        invite.refresh_from_db()
        self.assertEqual(invite.status, WatchInvite.Status.DECLINED)
        self.assertEqual(Suggestion.objects.count(), 0)
        second = self.invite()
        self.client.force_login(self.host)
        self.client.post(f"/invites/{second.pk}/cancel/")
        second.refresh_from_db()
        self.assertEqual(second.status, WatchInvite.Status.CANCELLED)

    def test_only_the_right_person_can_answer(self):
        invite = self.invite()
        self.assertEqual(self.client.post(f"/invites/{invite.pk}/accept/").status_code, 404)  # host cannot accept
        self.client.force_login(self.guest)
        self.assertEqual(self.client.post(f"/invites/{invite.pk}/cancel/").status_code, 404)  # guest cannot withdraw
        self.client.force_login(make_user("other"))
        self.assertEqual(self.client.post(f"/invites/{invite.pk}/accept/").status_code, 404)

    def test_today_shows_invites_both_ways(self):
        invite = self.invite()
        page = self.client.get("/dashboard/")
        self.assertContains(page, 'data-panel="invites"')
        self.assertContains(page, "waiting for them")
        self.client.force_login(self.guest)
        page = self.client.get("/dashboard/")
        self.assertContains(page, "wants to watch this with you")
        self.assertContains(page, f"/invites/{invite.pk}/accept/")


class ApiTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.token, self.raw = make_api_token(self.user, "Phone widget")

    def get(self, path, token=None, **params):
        return self.client.get(path, params, HTTP_AUTHORIZATION=f"Bearer {token or self.raw}")

    def test_tokens_are_hashed_and_shown_once(self):
        self.assertTrue(self.raw.startswith("mf_"))
        self.assertEqual(self.token.prefix, self.raw[:8])
        self.assertNotEqual(self.token.key_hash, self.raw)
        self.assertEqual(user_for_api_token(self.raw), self.user)
        self.assertIsNone(user_for_api_token("mf_nope"))
        self.assertIsNone(user_for_api_token(""))
        self.token.refresh_from_db()
        self.assertIsNotNone(self.token.last_used_at)

    def test_no_token_or_a_bad_one_is_401(self):
        response = self.client.get("/api/v1/me/")
        self.assertEqual(response.status_code, 401)
        self.assertIn("Bearer", response["WWW-Authenticate"])
        self.assertEqual(self.get("/api/v1/me/", token="mf_wrong").status_code, 401)
        # A session does not count: the API is tokens only.
        self.client.force_login(self.user)
        self.assertEqual(self.client.get("/api/v1/me/").status_code, 401)

    def test_me_backlog_day_and_progress(self):
        item = BacklogItem.objects.create(user=self.user, title="A book", media_type=MediaType.BOOK, estimated_minutes=600, minutes_completed=50)
        Suggestion.objects.create(
            user=self.user, backlog_item=item, date=TEST_DAY, start_time=time(19), end_time=time(20), minutes=60,
            status=Suggestion.Status.ACCEPTED,
        )
        me = self.get("/api/v1/me/").json()
        self.assertEqual((me["username"], me["day_start"]), ("sam", "08:00"))
        backlog = self.get("/api/v1/backlog/").json()["items"]
        self.assertEqual((backlog[0]["title"], backlog[0]["remaining_minutes"], backlog[0]["progress"]), ("A book", 550, "Chapter 3 of 24 next"))
        self.assertEqual(self.get("/api/v1/backlog/", status="completed").json()["items"], [])
        day = self.get("/api/v1/day/", date="2026-10-06").json()
        self.assertEqual(day["date"], "2026-10-06")
        self.assertEqual(day["planned"][0]["start_time"], "19:00")
        self.assertEqual(day["free"][0]["start"], "08:00")
        progress = self.get("/api/v1/progress/").json()
        self.assertEqual(progress["total_minutes"], 0)
        self.assertIn("current_streak", progress)

    def test_another_users_token_sees_only_their_data(self):
        other = make_user("other")
        BacklogItem.objects.create(user=other, title="Theirs", media_type=MediaType.BOOK, estimated_minutes=100)
        _, raw = make_api_token(other, "x")
        self.assertEqual([i["title"] for i in self.get("/api/v1/backlog/", token=raw).json()["items"]], ["Theirs"])
        self.assertEqual(self.get("/api/v1/backlog/").json()["items"], [])

    def test_tokens_are_made_and_revoked_from_settings(self):
        self.client.force_login(self.user)
        page = self.client.get("/settings/")
        self.assertContains(page, "Phone widget")
        self.assertContains(page, "/api/v1/")
        response = self.client.post("/settings/tokens/new/", {"name": "Script"})
        self.assertRedirects(response, "/settings/#api", fetch_redirect_response=False)
        page = self.client.get("/settings/")
        self.assertContains(page, "copy it now")
        self.assertContains(page, "mf_")
        token = ApiToken.objects.get(name="Script")
        self.client.post(f"/settings/tokens/{token.pk}/revoke/")
        self.assertFalse(ApiToken.objects.filter(pk=token.pk).exists())

    def test_revoking_someone_elses_token_is_404(self):
        self.client.force_login(make_user("other"))
        self.assertEqual(self.client.post(f"/settings/tokens/{self.token.pk}/revoke/").status_code, 404)
        self.assertTrue(ApiToken.objects.filter(pk=self.token.pk).exists())

    def test_a_deleted_token_stops_working(self):
        self.token.delete()
        self.assertEqual(self.get("/api/v1/me/").status_code, 401)


# ------------------------------------------------------------ iteration 4, part seven: the finishing polish


class ThemeAndNavTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.client.force_login(self.user)

    def test_the_theme_is_applied_before_paint_and_pickable(self):
        page = self.client.get("/dashboard/")
        self.assertContains(page, 'localStorage.getItem("mediaflow-theme")')
        self.assertContains(page, "js/theme.js")
        self.assertContains(page, 'data-theme-choice="system"')
        self.assertContains(page, 'data-theme-choice="light"')
        self.assertContains(page, 'data-theme-choice="dark"')
        self.assertContains(page, 'media="(prefers-color-scheme: light)"')
        self.assertContains(self.client.get("/settings/"), 'aria-label="Appearance"')

    def test_the_stylesheet_carries_both_palettes(self):
        source = (PROJECT_ROOT / "static" / "src" / "app.css").read_text(encoding="utf-8")
        self.assertIn('@media (prefers-color-scheme: light)', source)
        self.assertIn(':root[data-theme="light"]', source)
        self.assertIn(':root:not([data-theme="dark"])', source)
        built = (PROJECT_ROOT / "static" / "css" / "app.css").read_text(encoding="utf-8")
        self.assertIn("prefers-color-scheme:light", built)
        self.assertIn("data-theme=light", built)
        self.assertIn("var(--color-bg)", built)  # utilities read the variables, so the override reaches them

    def test_the_theme_script_exists_and_targets_the_picker(self):
        script = (PROJECT_ROOT / "static" / "js" / "theme.js").read_text(encoding="utf-8")
        self.assertIn("mediaflow-theme", script)
        self.assertIn("data-theme-choice", script)
        self.assertIn("aria-pressed", script)

    def test_the_nav_is_a_strip_on_phones_and_hides_nothing_essential(self):
        page = self.client.get("/dashboard/")
        self.assertContains(page, 'class="nav-strip')
        for href in ("/dashboard/", "/calendar/", "/backlog/", "/discover/", "/schedule/", "/reviews/", "/progress/", "/settings/"):
            self.assertContains(page, f'href="{href}"')
        self.assertContains(page, "Sign out")

    def test_tables_have_column_headers(self):
        self.assertContains(self.client.get("/calendar/"), '<th scope="col"')
        # The progress table only exists once something has been logged.
        item = BacklogItem.objects.create(user=self.user, title="A book", media_type=MediaType.BOOK, estimated_minutes=600)
        TimeLog.objects.create(user=self.user, backlog_item=item, date=timezone.localdate(), minutes=30)
        self.assertContains(self.client.get("/progress/"), 'scope="col"')

    def test_auto_submitting_selects_have_a_real_button(self):
        BacklogItem.objects.create(user=self.user, title="A book", media_type=MediaType.BOOK, estimated_minutes=600)
        self.assertContains(self.client.get("/backlog/"), "sr-only focus:not-sr-only")
        self.assertContains(self.client.get("/semester/"), "sr-only focus:not-sr-only")
        item = BacklogItem.objects.create(
            user=self.user, title="Done", media_type=MediaType.BOOK, estimated_minutes=100, status=BacklogItem.Status.COMPLETED
        )
        Review.objects.create(user=self.user, backlog_item=item, rating=4)
        self.assertContains(self.client.get("/reviews/"), "sr-only focus:not-sr-only")

    def test_no_text_is_smaller_than_ten_pixels(self):
        # 0.6rem at a 16px root is 9.6px; the calendar used to have it.
        for template in (PROJECT_ROOT / "templates" / "planner").glob("*.html"):
            self.assertNotIn("text-[0.6rem]", template.read_text(encoding="utf-8"), template.name)
