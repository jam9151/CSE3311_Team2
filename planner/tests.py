from datetime import date, datetime, time, timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from .forms import RegisterForm
from .models import BacklogItem, CatalogItem, Commitment, MediaType, Profile, RecurringBlock, Suggestion
from .services import day_strip, free_intervals, humanize_minutes, month_grid, suggest_for_day, total_free_minutes

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

    def test_day_strip_positions(self):
        user = make_user("strip")  # awake 8:00-23:00, so 15 hours wide
        RecurringBlock.objects.create(
            user=user, label="Class", weekday=TEST_DAY.weekday(), start_time=time(11), end_time=time(14)
        )
        strip = day_strip(user, TEST_DAY)
        seg = strip["segments"][0]
        # 11:00 is 3h into a 15h day, and the class is 3h long: 20% and 20%.
        self.assertEqual((seg["left"], seg["width"]), (20.0, 20.0))
        self.assertEqual(strip["ticks"][0], {"left": 0.0, "label": "8a"})

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
