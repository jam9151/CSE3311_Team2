from datetime import date, datetime, time, timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from .forms import RegisterForm
from .models import BacklogItem, CatalogItem, Commitment, MediaType, Profile, RecurringBlock, Suggestion
from .services import (
    free_intervals,
    humanize_minutes,
    month_grid,
    suggest_for_day,
    suggest_for_week,
    total_free_minutes,
    upcoming_reminders,
)

# A Tuesday. Most of these tests pin a fixed date because free_intervals()
# clips gaps that have already passed when the day is today, which would make
# the results depend on when you run the suite.
TEST_DAY = date(2026, 10, 6)


def make_user(username="sam", **profile_kwargs):
    user = get_user_model().objects.create_user(
        username=username, password="test-password", email=f"{username}@mavs.uta.edu"
    )
    Profile.objects.create(user=user, **{"day_start": time(8), "day_end": time(23), **profile_kwargs})
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
