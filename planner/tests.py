from datetime import date, datetime, time, timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from .forms import RegisterForm
from .models import BacklogItem, CatalogItem, Commitment, MediaType, Profile, RecurringBlock, Suggestion
from .services import free_intervals, humanize_minutes, month_grid, suggest_for_day, total_free_minutes


def make_user(username="sam", **profile_kwargs):
    user = get_user_model().objects.create_user(
        username=username, password="test-password", email=f"{username}@mavs.uta.edu"
    )
    Profile.objects.create(user=user, **{"day_start": time(8), "day_end": time(23), **profile_kwargs})
    return user


class FreeTimeDetectionTests(TestCase):
    """The core differentiator: gaps between commitments, not user-entered availability."""

    def setUp(self):
        self.user = make_user()
        # A Tuesday, so "today" logic does not clip the day under test.
        self.day = date(2026, 10, 6)

    def test_empty_day_is_one_long_gap(self):
        gaps = free_intervals(self.user, self.day)
        self.assertEqual(len(gaps), 1)
        self.assertEqual(gaps[0].start, time(8))
        self.assertEqual(gaps[0].end, time(23))
        self.assertEqual(gaps[0].minutes, 15 * 60)

    def test_recurring_block_splits_the_day(self):
        RecurringBlock.objects.create(
            user=self.user, label="CSE 3311", weekday=self.day.weekday(),
            start_time=time(11), end_time=time(12, 20),
        )
        gaps = free_intervals(self.user, self.day)
        self.assertEqual([(g.start, g.end) for g in gaps], [(time(8), time(11)), (time(12, 20), time(23))])

    def test_overlapping_blocks_merge_into_one_busy_span(self):
        RecurringBlock.objects.create(
            user=self.user, label="Work", weekday=self.day.weekday(), start_time=time(9), end_time=time(15)
        )
        RecurringBlock.objects.create(
            user=self.user, label="Meeting", weekday=self.day.weekday(), start_time=time(14), end_time=time(17)
        )
        gaps = free_intervals(self.user, self.day)
        self.assertEqual([(g.start, g.end) for g in gaps], [(time(8), time(9)), (time(17), time(23))])

    def test_dated_commitment_blocks_time(self):
        Commitment.objects.create(
            user=self.user, title="Group project",
            starts_at=timezone.make_aware(datetime(2026, 10, 6, 18, 0)),
            ends_at=timezone.make_aware(datetime(2026, 10, 6, 20, 0)),
        )
        gaps = free_intervals(self.user, self.day)
        self.assertEqual([(g.start, g.end) for g in gaps], [(time(8), time(18)), (time(20), time(23))])

    def test_slivers_shorter_than_the_minimum_are_dropped(self):
        RecurringBlock.objects.create(
            user=self.user, label="Class", weekday=self.day.weekday(), start_time=time(8, 10), end_time=time(23)
        )
        # Only a 10-minute sliver at the start of the day remains, which is unusable.
        self.assertEqual(free_intervals(self.user, self.day), [])

    def test_waking_hours_bound_the_search(self):
        self.user.profile.day_start = time(10)
        self.user.profile.day_end = time(20)
        self.user.profile.save()
        self.assertEqual(total_free_minutes(self.user, self.day), 10 * 60)


class SuggestionEngineTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.day = date(2026, 10, 6)

    def _evening_only(self, start=time(19), end=time(23)):
        """Leave exactly one free window, from `start` to `end`."""
        RecurringBlock.objects.create(
            user=self.user, label="Busy", weekday=self.day.weekday(), start_time=time(8), end_time=start
        )
        self.user.profile.day_end = end
        self.user.profile.save()

    def test_a_film_is_only_suggested_when_its_whole_runtime_fits(self):
        self._evening_only(time(21), time(23))  # two hours free
        BacklogItem.objects.create(
            user=self.user, title="Long film", media_type=MediaType.MOVIE, estimated_minutes=200, priority=5
        )
        self.assertEqual(suggest_for_day(self.user, self.day), [])

    def test_a_book_fills_a_gap_a_film_cannot(self):
        self._evening_only(time(21), time(23))
        BacklogItem.objects.create(
            user=self.user, title="Long film", media_type=MediaType.MOVIE, estimated_minutes=200, priority=5
        )
        book = BacklogItem.objects.create(
            user=self.user, title="A book", media_type=MediaType.BOOK, estimated_minutes=600, priority=2
        )
        suggestions = suggest_for_day(self.user, self.day)
        self.assertEqual(len(suggestions), 1)
        self.assertEqual(suggestions[0].backlog_item, book)
        # Capped at one realistic sitting (90 min) and rounded to whole
        # 25-minute chapters, rather than filling the entire two hours.
        self.assertEqual(suggestions[0].minutes, 75)

    def test_a_long_free_day_is_not_swallowed_by_one_item(self):
        self._evening_only(time(9), time(21))  # twelve hours free
        BacklogItem.objects.create(
            user=self.user, title="Doorstopper", media_type=MediaType.BOOK, estimated_minutes=3000, priority=5
        )
        suggestion = suggest_for_day(self.user, self.day)[0]
        # Three whole chapters: the 90-minute cap rounded down to 25-minute units.
        self.assertEqual(suggestion.minutes, 75)

    def test_a_nearly_finished_item_is_proposed_to_the_end(self):
        self._evening_only()
        BacklogItem.objects.create(
            user=self.user, title="Almost done", media_type=MediaType.BOOK,
            estimated_minutes=600, minutes_completed=560, priority=3,
        )
        suggestion = suggest_for_day(self.user, self.day)[0]
        self.assertEqual(suggestion.minutes, 40)
        self.assertIn("finish", suggestion.reason)

    def test_higher_priority_wins_when_both_fit(self):
        self._evening_only()
        low = BacklogItem.objects.create(
            user=self.user, title="Low", media_type=MediaType.BOOK, estimated_minutes=600, priority=1
        )
        high = BacklogItem.objects.create(
            user=self.user, title="High", media_type=MediaType.BOOK, estimated_minutes=600, priority=5
        )
        suggestions = suggest_for_day(self.user, self.day)
        self.assertEqual(suggestions[0].backlog_item, high)
        self.assertNotEqual(suggestions[0].backlog_item, low)

    def test_a_rejection_pushes_an_item_down_next_time(self):
        self._evening_only()
        rejected = BacklogItem.objects.create(
            user=self.user, title="Turned down", media_type=MediaType.BOOK, estimated_minutes=600, priority=5
        )
        other = BacklogItem.objects.create(
            user=self.user, title="Other", media_type=MediaType.BOOK, estimated_minutes=600, priority=4
        )
        # Priority 5 beats priority 4 on a clean slate.
        self.assertEqual(suggest_for_day(self.user, self.day)[0].backlog_item, rejected)

        Suggestion.objects.filter(backlog_item=rejected).update(status=Suggestion.Status.REJECTED)
        Suggestion.objects.filter(backlog_item=other).delete()

        self.assertEqual(suggest_for_day(self.user, self.day)[0].backlog_item, other)

    def test_completed_items_are_never_suggested(self):
        self._evening_only()
        BacklogItem.objects.create(
            user=self.user, title="Done", media_type=MediaType.BOOK, estimated_minutes=600,
            minutes_completed=600, status=BacklogItem.Status.COMPLETED, priority=5,
        )
        self.assertEqual(suggest_for_day(self.user, self.day), [])

    def test_accepted_suggestions_consume_free_time(self):
        self._evening_only()  # 19:00-23:00, four hours
        item = BacklogItem.objects.create(
            user=self.user, title="A book", media_type=MediaType.BOOK, estimated_minutes=600, priority=3
        )
        Suggestion.objects.create(
            user=self.user, backlog_item=item, date=self.day, start_time=time(19), end_time=time(21),
            minutes=120, status=Suggestion.Status.ACCEPTED,
        )
        gaps = free_intervals(self.user, self.day)
        self.assertEqual([(g.start, g.end) for g in gaps], [(time(21), time(23))])

    def test_each_suggestion_uses_a_different_item(self):
        # Two separate evening gaps, split by a dinner commitment.
        RecurringBlock.objects.create(
            user=self.user, label="Busy", weekday=self.day.weekday(), start_time=time(8), end_time=time(17)
        )
        Commitment.objects.create(
            user=self.user, title="Dinner",
            starts_at=timezone.make_aware(datetime(2026, 10, 6, 19, 0)),
            ends_at=timezone.make_aware(datetime(2026, 10, 6, 20, 0)),
        )
        for i in range(3):
            BacklogItem.objects.create(
                user=self.user, title=f"Book {i}", media_type=MediaType.BOOK, estimated_minutes=600, priority=3
            )
        suggestions = suggest_for_day(self.user, self.day)
        self.assertEqual(len(suggestions), 2)
        self.assertEqual(len({s.backlog_item_id for s in suggestions}), 2)


class HelperTests(TestCase):
    def test_humanize_minutes(self):
        self.assertEqual(humanize_minutes(45), "45 min")
        self.assertEqual(humanize_minutes(120), "2h")
        self.assertEqual(humanize_minutes(200), "3h 20m")

    def test_month_grid_starts_on_sunday_and_marks_the_month(self):
        user = make_user("grid")
        weeks = month_grid(user, 2026, 10)
        self.assertTrue(all(len(week) == 7 for week in weeks))
        # October 1 2026 is a Thursday, so the grid opens on Sunday September 27.
        self.assertEqual(weeks[0][0]["date"], date(2026, 9, 27))
        self.assertFalse(weeks[0][0]["in_month"])
        self.assertTrue(weeks[0][4]["in_month"])


class RegistrationTests(TestCase):
    def test_uta_email_is_required(self):
        form = RegisterForm(
            data={"username": "outsider", "email": "me@gmail.com",
                  "password1": "sturdy-passphrase-1", "password2": "sturdy-passphrase-1"}
        )
        self.assertFalse(form.is_valid())
        self.assertIn("email", form.errors)

    def test_mavs_email_is_accepted_and_creates_a_profile(self):
        form = RegisterForm(
            data={"username": "student", "email": "student@mavs.uta.edu",
                  "password1": "sturdy-passphrase-1", "password2": "sturdy-passphrase-1"}
        )
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

    def test_signed_in_pages_render(self):
        self.client.force_login(self.user)
        for url in ["/dashboard/", "/calendar/", "/backlog/", "/discover/", "/schedule/", "/reviews/"]:
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 200)

    def test_discover_filters_by_type_and_length(self):
        CatalogItem.objects.create(title="Short game", media_type=MediaType.GAME, typical_minutes=180)
        CatalogItem.objects.create(title="Epic game", media_type=MediaType.GAME, typical_minutes=3600)
        CatalogItem.objects.create(title="A film", media_type=MediaType.MOVIE, typical_minutes=120)

        self.client.force_login(self.user)
        response = self.client.get("/discover/", {"media_type": "game", "max_minutes": 200})
        self.assertContains(response, "Short game")
        self.assertNotContains(response, "Epic game")
        self.assertNotContains(response, "A film")

    def test_adding_from_the_catalog_creates_a_backlog_item(self):
        catalog_item = CatalogItem.objects.create(
            title="Portal 2", media_type=MediaType.GAME, typical_minutes=500
        )
        self.client.force_login(self.user)
        self.client.post(f"/media/{catalog_item.pk}/add/")
        entry = self.user.backlog_items.get()
        self.assertEqual(entry.title, "Portal 2")
        self.assertEqual(entry.estimated_minutes, 500)

    def test_accepting_a_suggestion_puts_it_on_the_calendar(self):
        item = BacklogItem.objects.create(
            user=self.user, title="A book", media_type=MediaType.BOOK, estimated_minutes=600
        )
        tomorrow = timezone.localdate() + timedelta(days=1)
        suggestion = Suggestion.objects.create(
            user=self.user, backlog_item=item, date=tomorrow,
            start_time=time(19), end_time=time(20), minutes=60,
        )
        self.client.force_login(self.user)
        self.client.post(f"/suggestions/{suggestion.pk}/accept/")

        suggestion.refresh_from_db()
        item.refresh_from_db()
        self.assertEqual(suggestion.status, Suggestion.Status.ACCEPTED)
        self.assertEqual(item.status, BacklogItem.Status.IN_PROGRESS)

    def test_marking_a_session_done_logs_progress(self):
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

    def test_a_user_cannot_touch_another_users_suggestion(self):
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
